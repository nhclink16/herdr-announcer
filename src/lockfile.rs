use rustix::fs::{FlockOperation, flock};
use std::fs::OpenOptions;
use std::io;
use std::path::Path;

pub fn with_flock<T>(lock: &Path, f: impl FnOnce() -> T) -> io::Result<T> {
    let file = OpenOptions::new().create(true).append(true).open(lock)?;
    flock(&file, FlockOperation::LockExclusive).map_err(io::Error::from)?;
    let result = f();
    flock(&file, FlockOperation::Unlock).map_err(io::Error::from)?;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn creates_and_reuses_a_real_lock_file() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("state.lock");
        assert_eq!(with_flock(&path, || 17).unwrap(), 17);
        assert_eq!(with_flock(&path, || 23).unwrap(), 23);
        assert!(path.is_file());
    }
}
