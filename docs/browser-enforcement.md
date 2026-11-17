# YouTube browser enforcement on Linux

Stopscroll installs a managed Manifest V3 extension in Chrome and Chromium. It uses the existing YouTube session and budget; it cannot grant time or change daemon state. `accounts.youtube.com` keeps its configured login exception. Other site groups, daily budgets and Telegram policy are unchanged.

The daemon atomically publishes a YouTube-only policy in `/run/stopscroll/browser.json` every tick. It contains domains, exceptions, the existing session deadline and a four-second freshness lease. A native messaging host reads only that public policy; it rejects all browser input and does not expose daemon commands or private state. Missing, invalid, expired or disconnected policy closes access.

The extension blocks network requests and replaces an already open YouTube document when access closes. A document-start guard also handles pages served by a YouTube service worker or CacheStorage. Default denies persist in declarative rules; temporary session allows are removed on closure or loss of freshness and disappear when the browser exits. Existing media is stopped when its document is replaced. Closing can take one daemon/host polling interval, up to the four-second freshness limit if publication stops.

Only the standard YouTube group origins have document/tab permissions. Additional custom domains added to that group receive network rules, but cached documents on arbitrary new origins do not gain content-script coverage automatically.

## Installation and recovery

Run the normal root installer. Chrome/Chromium and `openssl` are needed for this layer. The installer creates a private signing key at `/var/lib/stopscroll/browser-key.pem` (0600), packages the extension locally, and registers its native host. Neither the key nor a user profile belongs in Git. Upgrades reuse the key; increment the extension manifest version when extension code changes so Chrome installs the update.

A dedicated `stopscroll-youtube.json` policy uses **ExtensionSettings for this extension ID only**, preserving existing legacy ExtensionInstallForcelist entries such as the Playwright bridge. It references a local `file://` update manifest/CRX; no new listening server is needed. The installed extension is mandatory and cannot be disabled through Chrome's normal extension UI. Chrome refreshes managed policy asynchronously; verify the extension appears before declaring the browser protected. A browser restart is not required by the installation flow.

The daemon repairs its own policy/native-host registration during normal reconciliation. It does not edit another application's managed policy. The uninstaller removes only Stopscroll's registrations, package and signing key with the existing installation. Browser policy refresh then removes the mandatory extension. No cache, cookies, history or user profile are cleared.

Firefox retains existing DNS enforcement. Firefox WebsiteFilter and Chrome URLBlocklist only protect navigation; neither is a replacement for the extension's live-request and cached-document enforcement. This change does not install an unsigned Firefox add-on or claim equivalent Firefox protection.

## Verification

Run `python3 -m unittest discover -s tests -q` and `node --test browser/tests/worker.test.cjs`.

For optional real Chrome acceptance, install Playwright for Node and run `browser/tests/run-isolated.sh`. If Playwright lives outside the repository, set `PLAYWRIGHT_MODULE` to its module path. This diagnostic requires Node 20+, Chrome, openssl, Xvfb, dbus-run-session and passwordless sudo for `unshare -m`. It uses a private mount namespace, fresh HOME/profile, private DBus/display, self-signed local fixtures, and the owner's UID. It does not change live browser policies or budgets. The temporary fixture path is printed; it contains generated test keys and can be removed after inspection.

Acceptance checks cover a pre-existing mandatory extension alongside the guard, Google account exception, another site remaining untouched, closed navigation, valid-session media, closing a playing tab, service-worker cached content, fresh API blocking, session deadline and stale native policy. Worker tests separately cover native-port disconnection. Local fixture media is used because automated real YouTube playback has independent UMP/SABR failure modes.

The browser permissions cover normal profiles; users with root access, alternative browsers or disabled enterprise policy can bypass this layer. Native policy/API failures are fail-closed; this can temporarily close a valid session until the daemon/bridge recovers.

Primary references: [Chrome distribution on Linux](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions), [DNR service-worker limitations](https://developer.chrome.com/docs/extensions/reference/api/declarativeNetRequest), [Firefox WebsiteFilter source](https://raw.githubusercontent.com/mozilla-firefox/firefox/main/browser/components/enterprisepolicies/helpers/WebsiteFilter.sys.mjs).
