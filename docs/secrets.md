# Secrets

How services in this project get their credentials. The rules are the same for every
service: secrets never enter git, configs never hold a secret value, and an unattended
service must be able to start and read its secrets without a person present.

## The convention

- **Vaults:** one 1Password vault per purpose, named `Hermes-<Purpose>`.
- **Items:** one per service, named after the service.
- **Fields:** one per key, named after the key file the service expects.
- **Source of truth:** the vault. Credentials are fetched from it once, by the user, into the
  place the service reads at runtime.

## Config references: `keys/<service>/<name>`

Configs never contain a secret or an account-bound value directly. They refer to a fixed
key path, `keys/<service>/<name>`, and the service resolves that path at runtime.

- **Non-secret identifiers** (account, server, channel or workspace ids, hostnames) are
  versioned in the private integration repo and symlinked into `keys/`. They are
  identifiers, not credentials.
- **Secrets** are never versioned. They live in the key file (below).

## Unattended runtime read: the key file (primary)

Services that run unattended (launchd, gateways, watchers) cannot use the 1Password desktop
app, which needs a person to approve each access. The primary route is a **key file**:

1. The user fetches the secret from the vault once, with a script they run themselves, and
   writes it to `keys/<service>/<name>` with mode `0600`.
2. The key file is gitignored by the generic `keys/` rule. It never enters git.
3. The service reads the key file at runtime. The value is never printed or logged.
4. The service starts without any interactive wrapper. If the key file is missing, the
   service fails loudly rather than prompting.

Agent sessions do not call the 1Password CLI. Fetching into the key file is a user step.

## Optional: keychain-held service-account token

A read-only service-account token for one vault can also be kept in the macOS keychain, so
the service can fetch its own secrets. This route is optional and not the default.

- Keychain item: account `hermes`, service `hermes-op-<purpose-lowercase>`.
- Store it without the interactive prompt. The `security -w` prompt truncates input at about
  128 characters, and service-account tokens are longer. Pass the value directly, for example
  `-w "$(pbpaste)"`, and be aware the value then appears in the process arguments for a moment.
- A token that is stored truncated fails with a JSON decode error when the client starts.

## Setup (run by the user)

1. **Create the vault** in 1Password: `Hermes-<Purpose>`.
2. **Create the item** for the service and the field(s) named after its key file(s).
3. **Fetch into the key file.** Use a script you run in your own terminal. The secret must
   not appear in a chat, a commit, or a shell history you share.
4. **Check** the file exists and is non-empty, without printing it. Mode `0600`.

If you use the optional keychain route, also create a **read-only service account** for that
one vault (Developer, then Directory, then Access Tokens, then Service Account) and store its
token as described above.

## Rotating

Rotate a secret by changing its field in 1Password, then re-fetch it into the key file. For a
keychain-held token, create a new token, replace the item, and revoke the old token in 1Password.
