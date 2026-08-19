use std::env;
use std::path::{Path, PathBuf};

pub const PLUGIN_ID: &str = "nhclink16.announcer";

fn home_dir() -> PathBuf {
    env::var_os("HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}

pub fn local_plugin_dirs() -> (PathBuf, PathBuf) {
    local_plugin_dirs_from(&home_dir())
}

fn local_plugin_dirs_from(home: &Path) -> (PathBuf, PathBuf) {
    (
        home.join(".config")
            .join("herdr")
            .join("plugins")
            .join("config")
            .join(PLUGIN_ID),
        home.join(".local")
            .join("state")
            .join("herdr")
            .join("plugins")
            .join(PLUGIN_ID),
    )
}

pub fn resolve_dirs() -> (PathBuf, PathBuf) {
    let fallback = local_plugin_dirs();
    resolve_dirs_from(
        env::var_os("HERDR_PLUGIN_CONFIG_DIR"),
        env::var_os("HERDR_PLUGIN_STATE_DIR"),
        fallback,
    )
}

fn resolve_dirs_from(
    config: Option<std::ffi::OsString>,
    state: Option<std::ffi::OsString>,
    fallback: (PathBuf, PathBuf),
) -> (PathBuf, PathBuf) {
    let config = config.filter(|value| !value.is_empty()).map(PathBuf::from);
    let state = state.filter(|value| !value.is_empty()).map(PathBuf::from);
    (config.unwrap_or(fallback.0), state.unwrap_or(fallback.1))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsString;

    #[test]
    fn local_dirs_are_conventional_and_never_cwd() {
        let (config, state) = local_plugin_dirs_from(Path::new("/home/tester"));
        assert_eq!(
            config,
            Path::new("/home/tester/.config/herdr/plugins/config/nhclink16.announcer")
        );
        assert_eq!(
            state,
            Path::new("/home/tester/.local/state/herdr/plugins/nhclink16.announcer")
        );
        assert_ne!(state, Path::new("."));
    }

    #[test]
    fn both_environment_dirs_avoid_fallbacks() {
        let result = resolve_dirs_from(
            Some(OsString::from("/plugin/config")),
            Some(OsString::from("/plugin/state")),
            (
                PathBuf::from("/fallback/config"),
                PathBuf::from("/fallback/state"),
            ),
        );
        assert_eq!(
            result,
            (
                PathBuf::from("/plugin/config"),
                PathBuf::from("/plugin/state")
            )
        );
    }

    #[test]
    fn missing_state_dir_uses_only_state_fallback() {
        let result = resolve_dirs_from(
            Some(OsString::from("/plugin/config")),
            Some(OsString::new()),
            (
                PathBuf::from("/fallback/config"),
                PathBuf::from("/fallback/state"),
            ),
        );
        assert_eq!(
            result,
            (
                PathBuf::from("/plugin/config"),
                PathBuf::from("/fallback/state")
            )
        );
    }

    #[test]
    fn missing_config_dir_preserves_environment_state() {
        let result = resolve_dirs_from(
            Some(OsString::new()),
            Some(OsString::from("/plugin/state")),
            (
                PathBuf::from("/fallback/config"),
                PathBuf::from("/fallback/state"),
            ),
        );
        assert_eq!(
            result,
            (
                PathBuf::from("/fallback/config"),
                PathBuf::from("/plugin/state")
            )
        );
    }

    #[test]
    fn empty_environment_values_use_both_fallbacks() {
        let fallback = (
            PathBuf::from("/fallback/config"),
            PathBuf::from("/fallback/state"),
        );
        let result = resolve_dirs_from(
            Some(OsString::new()),
            Some(OsString::new()),
            fallback.clone(),
        );
        assert_eq!(result, fallback);
    }
}
