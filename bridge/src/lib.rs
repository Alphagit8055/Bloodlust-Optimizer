//! Bloodlust Optimizer — wire protocol and catalog verification.
//!
//! Split out from the Tauri shell on purpose. Nothing in here depends on Tauri,
//! a webview, or any GUI toolkit, which means:
//!
//! * `cargo test` runs on Linux CI with no display server or webkit,
//! * the JSON-RPC framing can be checked against the real Python sidecar in an
//!   integration test,
//! * and a bug in the protocol layer is found here rather than inside a
//!   window manager.
//!
//! # Layout
//!
//! [`protocol`] — constants shared with `sidecar/src/bloodlust`. The single
//! place where both sides' contract is written down.
//!
//! [`rpc`] — the JSON-RPC 2.0 client that spawns and talks to the Python
//! engine over stdio.
//!
//! [`catalog`] — HTTPS fetching, host allowlisting and SHA-256 provenance for
//! the catalog served from the GitHub repo.

pub mod catalog;
pub mod protocol;
pub mod rpc;

pub use catalog::{sha256_hex, validate_url, CatalogError, CatalogPayload, MAX_CATALOG_BYTES};
pub use protocol::{methods, APP_NAME, APP_VERSION, CATALOG_SCHEMA_VERSION, DEVELOPER};
pub use rpc::{catalog_load_params, RpcError, Sidecar};

/// Hosts the app is willing to fetch a catalog from.
///
/// Baked in at compile time rather than read from a config file: a value that
/// could be edited at runtime could be edited to point the app at an attacker's
/// server, which would defeat the point of having an allowlist.
pub const CATALOG_HOSTS: &[&str] = &[
    "githubusercontent.com", // raw.githubusercontent.com, the canonical raw URL
    "github.com",            // codeload / release assets
];

/// Build the catalog URL for a repository and ref.
///
/// Uses the `raw` form, which serves plain bytes with no HTML wrapper and no
/// rate-limit surprises on the web UI.
pub fn catalog_url(owner: &str, repo: &str, git_ref: &str, path: &str) -> String {
    format!(
        "https://raw.githubusercontent.com/{owner}/{repo}/{git_ref}/{path}"
    )
}

/// Build the updater endpoint for GitHub Releases.
///
/// `tauri-plugin-updater` expects a `latest.json` asset; the CI workflow
/// uploads one alongside the installer.
pub fn updater_endpoint(owner: &str, repo: &str) -> String {
    format!("https://github.com/{owner}/{repo}/releases/latest/download/latest.json")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn catalog_url_is_https_and_on_an_allowed_host() {
        let url = catalog_url("Alphagit8055", "Bloodlust-Optimizer", "main", "catalog/catalog.json");
        assert_eq!(
            url,
            "https://raw.githubusercontent.com/Alphagit8055/Bloodlust-Optimizer/main/catalog/catalog.json"
        );
        assert!(validate_url(&url, CATALOG_HOSTS).is_ok());
    }

    #[test]
    fn updater_endpoint_points_at_release_assets() {
        let url = updater_endpoint("Alphagit8055", "Bloodlust-Optimizer");
        assert!(url.starts_with("https://github.com/"));
        assert!(url.ends_with("/releases/latest/download/latest.json"));
    }

    #[test]
    fn every_default_catalog_host_is_allowed() {
        for host in CATALOG_HOSTS {
            let url = format!("https://{host}/some/path");
            assert!(validate_url(&url, CATALOG_HOSTS).is_ok(), "{host} rejected itself");
        }
    }
}
