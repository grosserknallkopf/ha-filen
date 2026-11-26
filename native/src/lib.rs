//! Python-facing bridge to the official Filen Rust SDK.
//!
//! The Home Assistant integration uses this module through `aiofilen`. Keeping the
//! bridge tiny means the integration is not a second implementation of the Filen
//! protocol: login, crypto, metadata and transfers all come from `filen-sdk-rs`.
//!
//! Transfers stay streaming. Uploads pull from a foreign `ByteSource` on a
//! dedicated thread with a bounded channel, downloads push into a foreign
//! `ByteSink` the same way. Neither side has to materialise a whole backup in
//! memory or on disk.

use std::{
    borrow::Cow,
    io,
    pin::Pin,
    str::FromStr,
    sync::Arc,
    task::{Context, Poll},
};

use filen_sdk_rs::{
    Error as SdkErrorOriginal, ErrorKind,
    auth::{Client, StringifiedClient, http::ClientConfig, unauth::UnauthClient},
    fs::{
        HasUUID,
        categories::{DirType, Normal},
        dir::RemoteDirectory,
        file::RemoteFile,
    },
    io::client_impl::IoSharedClientExt,
    user::UserInfo,
    util::MaybeSendCallback,
};
use filen_types::{auth::FileEncryptionVersion, fs::Uuid};
use futures::{AsyncRead, AsyncWrite, AsyncWriteExt};
use tokio_util::sync::PollSender;

uniffi::setup_scaffolding!();

/// One 1 MiB prefetch slot. Four slots keep a transfer moving without letting a
/// slow Python consumer pull an unbounded amount of plaintext into memory.
const TRANSFER_CHANNEL: usize = 4;
const READ_CHUNK: usize = 1024 * 1024;

// --------------------------------------------------------------------- errors

/// Error categories the Python layer maps back to `aiofilen` exceptions.
///
/// This is a flat UniFFI error: every variant carries only a human-readable
/// message (the fields are intentionally not exposed separately).
#[derive(Debug, uniffi::Error)]
#[uniffi(flat_error)]
pub enum SdkError {
    Auth(String),
    TwoFactor(String),
    Blocked(String),
    Connection(String),
    RateLimit(String),
    Quota(String),
    NotFound(String),
    Conflict(String),
    Integrity(String),
    Crypto(String),
    Stream(String),
    Other(String),
}

impl std::fmt::Display for SdkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Auth(message)
            | Self::TwoFactor(message)
            | Self::Blocked(message)
            | Self::Connection(message)
            | Self::RateLimit(message)
            | Self::Quota(message)
            | Self::NotFound(message)
            | Self::Conflict(message)
            | Self::Integrity(message)
            | Self::Crypto(message)
            | Self::Stream(message)
            | Self::Other(message) => f.write_str(message),
        }
    }
}

/// Turn an SDK error into a category the integration can act on.
///
/// The SDK already distinguishes the cases that matter (wrong password, 2FA,
/// storage full, missing file, ...). For plain `Server` errors we also inspect
/// the server code/message, because older API paths still use generic errors.
fn map_error(err: SdkErrorOriginal) -> SdkError {
    let message = err
        .server_message()
        .unwrap_or_else(|| err.message());
    match err.kind() {
        ErrorKind::Enter2fa | ErrorKind::Wrong2fa => SdkError::TwoFactor(message),
        ErrorKind::EmailOrPasswordWrong | ErrorKind::WrongPassword | ErrorKind::Unauthenticated => {
            SdkError::Auth(message)
        }
        ErrorKind::MaxStorageReached => SdkError::Quota(message),
        ErrorKind::FileNotFound | ErrorKind::FolderNotFound => SdkError::NotFound(message),
        ErrorKind::Reqwest | ErrorKind::RetryFailed | ErrorKind::TlsVerification => {
            SdkError::Connection(message)
        }
        ErrorKind::ContentHashMismatch => SdkError::Integrity(message),
        ErrorKind::Conversion
        | ErrorKind::MetadataWasNotDecrypted
        | ErrorKind::ChunkTooLarge
        | ErrorKind::InvalidType => SdkError::Crypto(message),
        ErrorKind::Cancelled => SdkError::Stream("transfer cancelled".to_string()),
        ErrorKind::InvalidName => SdkError::Conflict(message),
        ErrorKind::Server => {
            let code = err.server_code().unwrap_or_default();
            let text = format!("{code} {message}").to_lowercase();
            if text.contains("blocked") || text.contains("locked") || text.contains("banned") {
                SdkError::Blocked(message)
            } else if text.contains("2fa") || text.contains("two factor") {
                SdkError::TwoFactor(message)
            } else if text.contains("quota")
                || text.contains("storage")
                || text.contains("max_storage")
            {
                SdkError::Quota(message)
            } else if text.contains("not found") || text.contains("does not exist") {
                SdkError::NotFound(message)
            } else if text.contains("already exists") {
                SdkError::Conflict(message)
            } else if text.contains("password")
                || text.contains("api key")
                || text.contains("unauthorized")
            {
                SdkError::Auth(message)
            } else {
                SdkError::Other(message)
            }
        }
        _ => SdkError::Other(message),
    }
}

fn other(message: impl Into<String>) -> SdkError {
    SdkError::Other(message.into())
}

// --------------------------------------------------------------------- models

#[derive(Clone, Debug, uniffi::Record)]
pub struct Credentials {
    pub api_key: String,
    pub master_keys: Vec<String>,
    pub auth_version: u8,
    pub email: String,
    pub base_folder_uuid: String,
    pub user_id: u64,
    pub private_key: Option<String>,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct User {
    pub user_id: u64,
    pub email: String,
    pub max_storage: u64,
    pub storage_used: u64,
    pub base_folder_uuid: String,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct DriveFile {
    pub uuid: String,
    pub name: String,
    pub size: u64,
    pub mime: String,
    pub key: String,
    pub chunks: u64,
    pub bucket: String,
    pub region: String,
    pub parent_uuid: String,
    pub version: u8,
    pub last_modified: Option<i64>,
    pub creation: Option<i64>,
    pub timestamp: Option<i64>,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct DriveFolder {
    pub uuid: String,
    pub name: String,
    pub parent_uuid: Option<String>,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct DirListing {
    pub files: Vec<DriveFile>,
    pub folders: Vec<DriveFolder>,
}

#[derive(Clone, Debug, uniffi::Record)]
pub struct LoginResult {
    pub credentials: Credentials,
    pub user: User,
}

// ------------------------------------------------------------------- callbacks

/// One non-blocking read from a Python upload source.
///
/// An empty `data` value with `eof == false` means the Python event loop has
/// not produced the next piece yet. This distinction is essential: waiting in
/// a foreign Python callback can starve the event loop that must fill it.
#[derive(Clone, Debug, uniffi::Record)]
pub struct SourceRead {
    pub data: Vec<u8>,
    pub eof: bool,
}

/// Source of upload bytes, implemented by Python.
///
/// `read` is called from a dedicated Rust thread and must not block. Python
/// errors are kept on the Python object and turned into `FilenStreamError` by
/// the wrapper after the native call returns.
#[uniffi::export(with_foreign)]
pub trait ByteSource: Send + Sync {
    fn read(&self, max_bytes: u64) -> SourceRead;
}

/// Destination for download bytes, implemented by Python.
///
/// `write` returns `false` to stop the transfer (for example because the media
/// player closed the response). `finish` is called once after the last byte.
#[uniffi::export(with_foreign)]
pub trait ByteSink: Send + Sync {
    fn write(&self, data: Vec<u8>) -> bool;
    fn finish(&self);
}

#[uniffi::export(with_foreign)]
pub trait ProgressSink: Send + Sync {
    fn on_progress(&self, bytes: u64);
}

// --------------------------------------------------------------------- client

#[derive(uniffi::Object)]
pub struct SdkClient {
    inner: Arc<Client>,
}

#[uniffi::export(async_runtime = "tokio")]
impl SdkClient {
    #[uniffi::constructor]
    pub fn from_credentials(credentials: Credentials) -> Result<Arc<Self>, SdkError> {
        // Backups are uploaded as 1 MiB chunks. The SDK default of 16 simultaneous
        // transfers can create aggressive bursts on constrained HA hosts and home
        // routers; Fritz!Box telemetry showed bursts followed by long idle periods.
        // Limit this to four in-flight chunks while retaining some transfer overlap.
        let config = ClientConfig::default().with_concurrency(4);
        let unauth = UnauthClient::from_config(config).map_err(map_error)?;
        let auth_info = if credentials.auth_version == 3 {
            credentials
                .master_keys
                .first()
                .cloned()
                .unwrap_or_default()
        } else {
            credentials.master_keys.join("|")
        };
        if auth_info.is_empty() {
            return Err(SdkError::Auth(
                "stored credentials contain no master key".to_string(),
            ));
        }
        let private_key = credentials.private_key.ok_or_else(|| {
            SdkError::Auth("stored credentials contain no private key; log in again".to_string())
        })?;
        let client = unauth
            .from_stringified(StringifiedClient {
                email: credentials.email,
                user_id: credentials.user_id,
                root_uuid: credentials.base_folder_uuid,
                auth_info,
                private_key,
                api_key: credentials.api_key,
                auth_version: credentials.auth_version,
                max_parallel_requests: None,
                max_io_memory_usage: None,
            })
            .map_err(map_error)?;
        Ok(Arc::new(Self {
            inner: Arc::new(client),
        }))
    }

    pub fn credentials(&self) -> Credentials {
        credentials_from_client(&self.inner)
    }

    pub fn root_folder_uuid(&self) -> String {
        self.inner.root().uuid().to_string()
    }

    pub async fn user_info(&self) -> Result<User, SdkError> {
        self.inner
            .get_user_info()
            .await
            .map(user_from_info)
            .map_err(map_error)
    }

    pub async fn list_dir(&self, folder_uuid: String) -> Result<DirListing, SdkError> {
        let dir = dir_type(&self.inner, &folder_uuid).await?;
        let (folders, files) = self
            .inner
            .list_dir::<fn(u64, Option<u64>), Normal>(&dir, None)
            .await
            .map_err(map_error)?;
        Ok(DirListing {
            files: files.into_iter().map(file_from_sdk).collect(),
            folders: folders.into_iter().map(folder_from_sdk).collect(),
        })
    }

    pub async fn create_folder(
        &self,
        parent_uuid: String,
        name: String,
    ) -> Result<DriveFolder, SdkError> {
        let parent = dir_type(&self.inner, &parent_uuid).await?;
        self.inner
            .create_dir(&parent, &name)
            .await
            .map(folder_from_sdk)
            .map_err(map_error)
    }

    pub async fn ensure_folder(
        &self,
        parent_uuid: String,
        name: String,
    ) -> Result<DriveFolder, SdkError> {
        let parent = dir_type(&self.inner, &parent_uuid).await?;
        if let Some(uuid) = self
            .inner
            .find_dir_uuid(parent.uuid(), &name)
            .await
            .map_err(map_error)?
        {
            return self
                .inner
                .get_dir(uuid)
                .await
                .map(folder_from_sdk)
                .map_err(map_error);
        }
        self.inner
            .create_dir(&parent, &name)
            .await
            .map(folder_from_sdk)
            .map_err(map_error)
    }

    pub async fn find_folder(
        &self,
        parent_uuid: String,
        name: String,
    ) -> Result<Option<DriveFolder>, SdkError> {
        let parent = dir_type(&self.inner, &parent_uuid).await?;
        let Some(uuid) = self
            .inner
            .find_dir_uuid(parent.uuid(), &name)
            .await
            .map_err(map_error)?
        else {
            return Ok(None);
        };
        self.inner
            .get_dir(uuid)
            .await
            .map(|dir| Some(folder_from_sdk(dir)))
            .map_err(map_error)
    }

    pub async fn file_meta(&self, file_uuid: String) -> Result<DriveFile, SdkError> {
        let uuid = parse_uuid(&file_uuid)?;
        self.inner
            .get_file(uuid)
            .await
            .map(file_from_sdk)
            .map_err(map_error)
    }

    pub async fn trash_file(&self, file_uuid: String) -> Result<(), SdkError> {
        let uuid = parse_uuid(&file_uuid)?;
        let mut file = self.inner.get_file(uuid).await.map_err(map_error)?;
        self.inner.trash_file(&mut file).await.map_err(map_error)
    }

    pub async fn delete_file(&self, file_uuid: String) -> Result<(), SdkError> {
        let uuid = parse_uuid(&file_uuid)?;
        let file = self.inner.get_file(uuid).await.map_err(map_error)?;
        self.inner
            .delete_file_permanently(file)
            .await
            .map_err(map_error)
    }

    pub async fn upload(
        &self,
        source: Arc<dyn ByteSource>,
        size: u64,
        folder_uuid: String,
        name: String,
        mime: String,
        progress: Option<Arc<dyn ProgressSink>>,
    ) -> Result<DriveFile, SdkError> {
        let parent = parse_uuid(&folder_uuid)?;
        let builder = self
            .inner
            .make_file_builder(&name, parent)
            .map_err(|err| SdkError::Conflict(err.to_string()))?
            .mime(mime);
        let mut reader = CallbackReader::new(source);
        let callback: Option<MaybeSendCallback<'_, u64>> = progress.map(|progress| {
            Arc::new(move |bytes: u64| progress.on_progress(bytes))
                as Arc<dyn Fn(u64) + Send + Sync>
        });
        self.inner
            .upload_file_from_reader(builder, &mut reader, callback, Some(size))
            .await
            .map(file_from_sdk)
            .map_err(map_error)
    }

    pub async fn download(
        &self,
        file_uuid: String,
        start: u64,
        end: u64,
        sink: Arc<dyn ByteSink>,
        progress: Option<Arc<dyn ProgressSink>>,
    ) -> Result<(), SdkError> {
        let uuid = parse_uuid(&file_uuid)?;
        let file = self.inner.get_file(uuid).await.map_err(map_error)?;
        let mut writer = CallbackWriter::new(sink);
        let callback: Option<MaybeSendCallback<'_, u64>> = progress.map(|progress| {
            Arc::new(move |bytes: u64| progress.on_progress(bytes))
                as Arc<dyn Fn(u64) + Send + Sync>
        });
        let result = self
            .inner
            .download_file_to_writer_for_range(&file, &mut writer, callback, start, end)
            .await;
        // The SDK closes the writer on success. On an error it may not have
        // done so; closing here releases the drain thread in every case.
        let _ = writer.close().await;
        let sink_result = writer.join();
        match (result, sink_result) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(err), _) => Err(map_error(err)),
            (Ok(()), Err(err)) => Err(err),
        }
    }
}

#[uniffi::export(async_runtime = "tokio")]
pub async fn login(
    email: String,
    password: String,
    two_factor_code: Option<String>,
) -> Result<LoginResult, SdkError> {
    let unauth = UnauthClient::from_config(ClientConfig::default()).map_err(map_error)?;
    let client = unauth
        .login(
            email,
            &password,
            two_factor_code.as_deref().unwrap_or("XXXXXX"),
        )
        .await
        .map_err(map_error)?;
    let user = client.get_user_info().await.map_err(map_error)?;
    Ok(LoginResult {
        credentials: credentials_from_client(&client),
        user: user_from_info(user),
    })
}

// --------------------------------------------------------------------- helpers

fn parse_uuid(value: &str) -> Result<Uuid, SdkError> {
    Uuid::from_str(value).map_err(|err| other(format!("invalid UUID {value:?}: {err}")))
}

async fn dir_type(
    client: &Client,
    folder_uuid: &str,
) -> Result<DirType<'static, Normal>, SdkError> {
    let root = client.root();
    if folder_uuid == root.uuid().to_string() {
        return Ok(DirType::Root(Cow::Owned(root.clone())));
    }
    let uuid = parse_uuid(folder_uuid)?;
    let dir = client.get_dir(uuid).await.map_err(map_error)?;
    Ok(DirType::Dir(Cow::Owned(dir)))
}

fn credentials_from_client(client: &Client) -> Credentials {
    let stringified = client.to_stringified();
    let master_keys = if stringified.auth_version == 3 {
        vec![stringified.auth_info]
    } else {
        stringified
            .auth_info
            .split('|')
            .filter(|key| !key.is_empty())
            .map(str::to_owned)
            .collect()
    };
    Credentials {
        api_key: stringified.api_key,
        master_keys,
        auth_version: stringified.auth_version,
        email: stringified.email,
        base_folder_uuid: stringified.root_uuid,
        user_id: stringified.user_id,
        private_key: Some(stringified.private_key),
    }
}

fn user_from_info(info: UserInfo) -> User {
    User {
        user_id: info.id,
        email: info.email,
        max_storage: info.max_storage,
        storage_used: info.storage_used,
        base_folder_uuid: info.root_dir_uuid.to_string(),
    }
}

fn file_from_sdk(file: RemoteFile) -> DriveFile {
    let meta = &file.meta;
    let key = meta.key();
    let version = key
        .map(|key| key.version())
        .unwrap_or(FileEncryptionVersion::V2) as u8;
    DriveFile {
        uuid: file.uuid.to_string(),
        name: meta.name().unwrap_or_default().to_string(),
        size: file.size,
        mime: meta
            .mime()
            .unwrap_or("application/octet-stream")
            .to_string(),
        key: key.map(ToString::to_string).unwrap_or_default(),
        chunks: file.chunks,
        bucket: file.bucket,
        region: file.region,
        parent_uuid: file.parent.to_string(),
        version,
        last_modified: meta.last_modified().map(|time| time.timestamp_millis()),
        creation: meta.created().map(|time| time.timestamp_millis()),
        timestamp: Some(file.timestamp.timestamp_millis()),
    }
}

fn folder_from_sdk(dir: RemoteDirectory) -> DriveFolder {
    DriveFolder {
        uuid: dir.uuid.to_string(),
        name: dir.meta.name().unwrap_or_default().to_string(),
        parent_uuid: Some(dir.parent.to_string()),
    }
}

// ------------------------------------------------------------------- streaming

/// Pulls bytes from Python on a dedicated thread and exposes them as
/// `AsyncRead`. `blocking_send` is what gives the Python producer backpressure:
/// when the SDK stops reading, the channel fills and the producer waits.
struct CallbackReader {
    receiver: tokio::sync::mpsc::Receiver<Vec<u8>>,
    leftover: Vec<u8>,
    offset: usize,
    done: bool,
    _thread: std::thread::JoinHandle<()>,
}

impl CallbackReader {
    fn new(source: Arc<dyn ByteSource>) -> Self {
        let (sender, receiver) = tokio::sync::mpsc::channel::<Vec<u8>>(TRANSFER_CHANNEL);
        let thread = std::thread::spawn(move || {
            loop {
                let read = source.read(READ_CHUNK as u64);
                if read.eof {
                    let _ = sender.blocking_send(Vec::new());
                    break;
                }
                if read.data.is_empty() {
                    // Never wait inside the Python callback. The Python
                    // producer runs on its event loop and may need the GIL to
                    // enqueue its first or next byte.
                    std::thread::sleep(std::time::Duration::from_millis(5));
                    continue;
                }
                if sender.blocking_send(read.data).is_err() {
                    break;
                }
            }
        });
        Self {
            receiver,
            leftover: Vec::new(),
            offset: 0,
            done: false,
            _thread: thread,
        }
    }
}

impl AsyncRead for CallbackReader {
    fn poll_read(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &mut [u8],
    ) -> Poll<io::Result<usize>> {
        if self.done {
            return Poll::Ready(Ok(0));
        }
        loop {
            if self.offset < self.leftover.len() {
                let available = self.leftover.len() - self.offset;
                let count = available.min(buf.len());
                buf[..count].copy_from_slice(&self.leftover[self.offset..self.offset + count]);
                self.offset += count;
                if self.offset == self.leftover.len() {
                    self.leftover.clear();
                    self.offset = 0;
                }
                return Poll::Ready(Ok(count));
            }
            match self.receiver.poll_recv(cx) {
                Poll::Ready(Some(data)) if data.is_empty() => {
                    self.done = true;
                    return Poll::Ready(Ok(0));
                }
                Poll::Ready(Some(data)) => {
                    self.leftover = data;
                    self.offset = 0;
                }
                Poll::Ready(None) => {
                    self.done = true;
                    return Poll::Ready(Ok(0));
                }
                Poll::Pending => return Poll::Pending,
            }
        }
    }
}

/// Sends bytes from the SDK to Python on a dedicated thread. `PollSender`
/// applies backpressure through the bounded channel.
struct CallbackWriter {
    sender: PollSender<Vec<u8>>,
    handle: Option<std::thread::JoinHandle<Result<(), SdkError>>>,
    closed: bool,
}

impl CallbackWriter {
    fn new(sink: Arc<dyn ByteSink>) -> Self {
        let (sender, mut receiver) = tokio::sync::mpsc::channel::<Vec<u8>>(TRANSFER_CHANNEL);
        let handle = std::thread::spawn(move || {
            while let Some(chunk) = receiver.blocking_recv() {
                if !sink.write(chunk) {
                    return Err(SdkError::Stream("download sink stopped".to_string()));
                }
            }
            sink.finish();
            Ok(())
        });
        Self {
            sender: PollSender::new(sender),
            handle: Some(handle),
            closed: false,
        }
    }

    fn join(mut self) -> Result<(), SdkError> {
        match self.handle.take() {
            Some(handle) => handle.join().unwrap_or_else(|_| {
                Err(SdkError::Stream(
                    "download worker thread panicked".to_string(),
                ))
            }),
            None => Ok(()),
        }
    }
}

impl AsyncWrite for CallbackWriter {
    fn poll_write(
        mut self: Pin<&mut Self>,
        cx: &mut Context<'_>,
        buf: &[u8],
    ) -> Poll<io::Result<usize>> {
        if self.closed {
            return Poll::Ready(Err(io::Error::from(io::ErrorKind::BrokenPipe)));
        }
        match self.sender.poll_reserve(cx) {
            Poll::Ready(Ok(())) => {
                if self.sender.send_item(buf.to_vec()).is_err() {
                    self.closed = true;
                    Poll::Ready(Err(io::Error::from(io::ErrorKind::BrokenPipe)))
                } else {
                    Poll::Ready(Ok(buf.len()))
                }
            }
            Poll::Ready(Err(_)) => {
                self.closed = true;
                Poll::Ready(Err(io::Error::from(io::ErrorKind::BrokenPipe)))
            }
            Poll::Pending => Poll::Pending,
        }
    }

    fn poll_flush(self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        Poll::Ready(Ok(()))
    }

    fn poll_close(mut self: Pin<&mut Self>, _cx: &mut Context<'_>) -> Poll<io::Result<()>> {
        self.sender.close();
        self.closed = true;
        Poll::Ready(Ok(()))
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{
        Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    };

    use futures::{AsyncReadExt, AsyncWriteExt};

    use super::*;

    struct Source {
        data: Vec<u8>,
        offset: Mutex<usize>,
    }

    struct DelayedSource {
        empty_reads: AtomicUsize,
        data: Vec<u8>,
        offset: Mutex<usize>,
    }

    impl ByteSource for DelayedSource {
        fn read(&self, max_bytes: u64) -> SourceRead {
            if self
                .empty_reads
                .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |count| {
                    count.checked_sub(1)
                })
                .is_ok()
            {
                return SourceRead {
                    data: Vec::new(),
                    eof: false,
                };
            }
            let mut offset = self.offset.lock().unwrap();
            let start = *offset;
            let end = (start + usize::try_from(max_bytes).unwrap()).min(self.data.len());
            *offset = end;
            SourceRead {
                data: self.data[start..end].to_vec(),
                eof: start == self.data.len(),
            }
        }
    }

    impl ByteSource for Source {
        fn read(&self, max_bytes: u64) -> SourceRead {
            let mut offset = self.offset.lock().unwrap();
            let start = *offset;
            let end = (start + usize::try_from(max_bytes).unwrap()).min(self.data.len());
            *offset = end;
            SourceRead {
                data: self.data[start..end].to_vec(),
                eof: start == self.data.len(),
            }
        }
    }

    #[derive(Default)]
    struct Sink {
        data: Mutex<Vec<u8>>,
        finished: AtomicBool,
    }

    impl ByteSink for Sink {
        fn write(&self, data: Vec<u8>) -> bool {
            self.data.lock().unwrap().extend(data);
            true
        }

        fn finish(&self) {
            self.finished.store(true, Ordering::SeqCst);
        }
    }

    #[test]
    fn callback_reader_yields_every_byte() {
        let data: Vec<u8> = (0..=255u8)
            .cycle()
            .take(5 * 1024 * 1024 + 123)
            .collect();
        let source = Arc::new(Source {
            data: data.clone(),
            offset: Mutex::new(0),
        });
        let mut reader = CallbackReader::new(source);
        let mut out = Vec::new();
        futures::executor::block_on(reader.read_to_end(&mut out)).unwrap();
        assert_eq!(out, data);
    }

    #[test]
    fn callback_reader_waits_for_a_nonblocking_source() {
        let data = b"delayed upload".to_vec();
        let source = Arc::new(DelayedSource {
            empty_reads: AtomicUsize::new(3),
            data: data.clone(),
            offset: Mutex::new(0),
        });
        let mut reader = CallbackReader::new(source);
        let mut out = Vec::new();
        futures::executor::block_on(reader.read_to_end(&mut out)).unwrap();
        assert_eq!(out, data);
    }

    #[test]
    fn callback_writer_preserves_bytes_and_finishes() {
        let sink = Arc::new(Sink::default());
        let mut writer = CallbackWriter::new(sink.clone());
        let data = (0..=255u8).cycle().take(2 * 1024 * 1024 + 7).collect::<Vec<_>>();
        futures::executor::block_on(async {
            writer.write_all(&data).await.unwrap();
            writer.close().await.unwrap();
        });
        writer.join().unwrap();
        assert_eq!(*sink.data.lock().unwrap(), data);
        assert!(sink.finished.load(Ordering::SeqCst));
    }
}
