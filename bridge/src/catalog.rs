//! Catalog fetching and verification.
//!
//! The catalog lives in the GitHub repo and is fetched over HTTPS. This module
//! is the entire trust boundary for that, so it enforces:
//!
//! * **HTTPS only.** An `http://` URL is rejected outright.
//! * **Host allowlist.** Only hosts the build was configured for are fetched.
//!   This stops a config mistake (or a tampered config) from pointing the app
//!   at an attacker's server that serves a hostile catalog.
//! * **Size cap, applied while streaming**, so a huge response cannot exhaust
//!   memory before it is rejected.
//! * **SHA-256 over the exact bytes**, reported to the sidecar and shown in the
//!   UI as provenance.
//!
//! What this does *not* do is decide whether a catalog is acceptable — that is
//! the sidecar's job, because only the sidecar knows which handlers it ships.
//! A catalog that passes every check here can still be rejected there, and the
//! bundled copy stays in use.

use sha2::{Digest, Sha256};

use thiserror::Error;

/// Default cap. A catalog is a few tens of KB; this is generous headroom.
pub const MAX_CATALOG_BYTES: usize = 4 * 1024 * 1024;

/// Read incrementally so the cap is enforced before the body is buffered.
const READ_CHUNK: usize = 64 * 1024;

#[derive(Debug, Error)]
pub enum CatalogError {
    #[error("refusing to fetch {0}: only https URLs are allowed")]
    InsecureScheme(String),

    #[error("refusing to fetch {url}: host is not in the allowlist")]
    HostNotAllowed { url: String },

    #[error("catalog URL has no parseable host: {0}")]
    BadUrl(String),

    #[error("catalog is larger than the {limit} byte limit")]
    TooLarge { limit: usize },

    #[error("failed to fetch the catalog: {0}")]
    Fetch(String),

    #[error("catalog is empty")]
    Empty,
}

/// Where a catalog came from, and enough information to prove it.
#[derive(Debug, Clone)]
pub struct CatalogPayload {
    pub bytes: Vec<u8>,
    pub sha256: String,
    pub url: String,
}

/// Check the URL before any connection is made.
///
/// Deliberately a separate function: it is pure, so it can be unit tested for
/// every rejection case without a network.
pub fn validate_url(url: &str, allowed_hosts: &[&str]) -> Result<(), CatalogError> {
    let lower = url.to_ascii_lowercase();

    if !lower.starts_with("https://") {
        return Err(CatalogError::InsecureScheme(url.to_string()));
    }

    let after_scheme = &url["https://".len()..];
    let host = after_scheme
        .split(['/', '?', '#'])
        .next()
        .unwrap_or("")
        // Strip any userinfo and port.
        .rsplit_once('@')
        .map(|(_, h)| h)
        .unwrap_or(after_scheme.split(['/', '?', '#']).next().unwrap_or(""))
        .split(':')
        .next()
        .unwrap_or("");

    if host.is_empty() {
        return Err(CatalogError::BadUrl(url.to_string()));
    }

    let host = host.to_ascii_lowercase();
    if allowed_hosts
        .iter()
        .any(|allowed| host == *allowed || host.ends_with(&format!(".{allowed}")))
    {
        Ok(())
    } else {
        Err(CatalogError::HostNotAllowed {
            url: url.to_string(),
        })
    }
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    let digest = hasher.finalize();
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

/// Fetch and verify. Requires the `http` feature; the Tauri shell enables it.
#[cfg(feature = "http")]
pub fn fetch(url: &str, allowed_hosts: &[&str]) -> Result<CatalogPayload, CatalogError> {
    validate_url(url, allowed_hosts)?;

    let agent = ureq::builder()
        .timeout_connect(std::time::Duration::from_secs(10))
        .timeout_read(std::time::Duration::from_secs(20))
        .build();
    let response = agent
        .get(url)
        .call()
        .map_err(|e| CatalogError::Fetch(e.to_string()))?;

    let declared = response
        .header("content-length")
        .and_then(|v| v.parse::<usize>().ok());
    if let Some(len) = declared {
        if len > MAX_CATALOG_BYTES {
            return Err(CatalogError::TooLarge {
                limit: MAX_CATALOG_BYTES,
            });
        }
    }

    // Stream and cap, so a lying or absent content-length cannot OOM us.
    let mut reader = response.into_reader().take(MAX_CATALOG_BYTES as u64 + 1);
    let mut bytes = Vec::with_capacity(declared.unwrap_or(64 * 1024).min(MAX_CATALOG_BYTES));
    let mut buf = vec![0u8; READ_CHUNK];
    loop {
        use std::io::Read;
        let n = reader
            .read(&mut buf)
            .map_err(|e| CatalogError::Fetch(e.to_string()))?;
        if n == 0 {
            break;
        }
        bytes.extend_from_slice(&buf[..n]);
        if bytes.len() > MAX_CATALOG_BYTES {
            return Err(CatalogError::TooLarge {
                limit: MAX_CATALOG_BYTES,
            });
        }
    }

    if bytes.is_empty() {
        return Err(CatalogError::Empty);
    }

    Ok(CatalogPayload {
        sha256: sha256_hex(&bytes),
        bytes,
        url: url.to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    const HOSTS: &[&str] = &["githubusercontent.com", "github.com"];

    // ------------------------------------------------------------- allowlist

    #[test]
    fn accepts_the_canonical_raw_github_url() {
        let url = "https://raw.githubusercontent.com/owner/repo/main/catalog/catalog.json";
        assert!(validate_url(url, HOSTS).is_ok());
    }

    #[test]
    fn accepts_a_subdomain_of_an_allowed_host() {
        assert!(validate_url("https://codeload.github.com/owner/repo", HOSTS).is_ok());
    }

    #[test]
    fn rejects_a_lookalike_host() {
        // Suffix matching must be dot-anchored, not a substring match.
        assert!(matches!(
            validate_url("https://githubusercontent.com.evil.test/c.json", HOSTS),
            Err(CatalogError::HostNotAllowed { .. })
        ));
        assert!(matches!(
            validate_url("https://notgithubusercontent.com/c.json", HOSTS),
            Err(CatalogError::HostNotAllowed { .. })
        ));
    }

    #[test]
    fn rejects_plain_http() {
        assert!(matches!(
            validate_url("http://raw.githubusercontent.com/o/r/main/c.json", HOSTS),
            Err(CatalogError::InsecureScheme(_))
        ));
    }

    #[test]
    fn rejects_a_missing_scheme() {
        assert!(matches!(
            validate_url("raw.githubusercontent.com/o/r/main/c.json", HOSTS),
            Err(CatalogError::InsecureScheme(_))
        ));
    }

    #[test]
    fn rejects_credentials_in_the_url() {
        // A userinfo section must not let a hostile host masquerade as ours.
        assert!(matches!(
            validate_url("https://raw.githubusercontent.com@evil.test/c.json", HOSTS),
            Err(CatalogError::HostNotAllowed { .. })
        ));
    }

    #[test]
    fn rejects_an_empty_host() {
        assert!(matches!(
            validate_url("https:///catalog.json", HOSTS),
            Err(CatalogError::BadUrl(_))
        ));
    }

    #[test]
    fn allows_a_port_on_an_allowed_host() {
        assert!(validate_url("https://github.com:8443/o/r/c.json", HOSTS).is_ok());
    }

    #[test]
    fn host_matching_is_case_insensitive() {
        assert!(validate_url("https://RAW.GitHubUserContent.com/o/r/c.json", HOSTS).is_ok());
    }

    // ------------------------------------------------------------------ hash

    #[test]
    fn sha256_matches_a_known_vector() {
        // SHA-256 of the empty string, a fixed published value.
        assert_eq!(
            sha256_hex(b""),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
        assert_eq!(
            sha256_hex(b"abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn sha256_is_lowercase_hex_of_fixed_length() {
        let hex = sha256_hex(b"some catalog bytes");
        assert_eq!(hex.len(), 64);
        assert!(hex.chars().all(|c| c.is_ascii_hexdigit() && !c.is_ascii_uppercase()));
    }

    #[test]
    fn sha256_changes_when_one_byte_changes() {
        assert_ne!(sha256_hex(b"{\"schema\":1}"), sha256_hex(b"{\"schema\":2}"));
    }

    #[test]
    fn cap_is_a_sane_value() {
        assert_eq!(MAX_CATALOG_BYTES, 4 * 1024 * 1024);
        assert!(READ_CHUNK < MAX_CATALOG_BYTES);
    }
}
