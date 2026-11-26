#!/bin/bash
# Import an RPM signing key only when every key in it is pinned (roles/repos_dnf/tasks/main.yml: the Red Hat internal,
# RPM Fusion and Tailscale keys). Every input comes from the environment, never from templated script text; the tasks
# compute each value inline (pins read from the tracked defaults file), with no block or task var in between:
#   KEY_URL  https URL of an ASCII-armored public key block
#   PINNED   space-separated 40-hex primary fingerprints the file may hold
#   PIN_VAR  where the pins live, for messages
#   DEST     optional: also install the verified key file there, for a repo's file:// gpgkey
# The key's fingerprints are read by both gpg and rpm (in a scratch rpmdb): rpm is what trusts the key, and the two
# parsers disagree on some inputs (a secret key relabelled as a public key block is "sec" to gpg but a public key to
# rpm), so the file is refused unless both see the same keys and every one of them is pinned.
# Only primary fingerprints are compared. A foreign subkey grafted onto a pinned primary key passes this check, but it
# carries no binding signature from that primary key, so rpm (rpm-sequoia) rejects the subkey and never trusts
# signatures made with it.
set -euo pipefail
: "${KEY_URL:?}" "${PINNED:?}"
DEST=${DEST:-}
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
PIN_VAR=${PIN_VAR:-the pinned fingerprint}

refuse() {
  echo "SECURITY: $*" >&2
  exit 3
}

pins=" "
for fp in $PINNED; do
  fp=$(printf '%s' "$fp" | tr 'a-f' 'A-F')
  [[ $fp =~ ^[0-9A-F]{40}$ ]] || refuse "pinned fingerprint '$fp' in $PIN_VAR is not 40 hex digits"
  pins+="$fp "
done
[ "$pins" != " " ] || refuse "no fingerprint is pinned in $PIN_VAR"

VERIFIED=""
# verify FILE: sets VERIFIED to the file's primary fingerprints, or refuses
verify() {
  local file=$1 home db gpg_fps rpm_ids id fp match
  [ "$(head -n 1 "$file" | tr -d '\r\000')" = "-----BEGIN PGP PUBLIC KEY BLOCK-----" ] ||
    refuse "the key at $KEY_URL is not an ASCII-armored public key block (binary keys are not accepted)"
  home=$(mktemp -d "$WORKDIR/gnupg.XXXXXX")
  local listing
  listing=$(gpg --batch --homedir "$home" --show-keys --with-colons "$file" 2>/dev/null) ||
    refuse "gpg cannot read the key at $KEY_URL"
  if grep -qE '^(sec|ssb):' <<<"$listing"; then
    refuse "the key at $KEY_URL holds secret key material"
  fi
  gpg_fps=$(awk -F: '$1 == "pub" {primary=1} $1 == "sub" {primary=0} $1 == "fpr" && primary {print $10; primary=0}' \
    <<<"$listing" | tr 'a-f' 'A-F' | sort -u)
  [ -n "$gpg_fps" ] || refuse "the key at $KEY_URL holds no public key"
  db=$(mktemp -d "$WORKDIR/rpmdb.XXXXXX")
  local rpmdefs=(--dbpath "$db" --define "_keyring rpmdb" --define "_keyringpath $db/pubkeys")
  rpm "${rpmdefs[@]}" --import "$file" 2>/dev/null || refuse "rpm does not accept the key at $KEY_URL as public keys only"
  rpm_ids=$(rpm "${rpmdefs[@]}" -q gpg-pubkey --qf '%{VERSION}\n' 2>/dev/null | tr 'a-f' 'A-F' | sort -u) || true
  # rpm 6 reports full fingerprints, older rpm 8-hex key IDs: match each against the end of a gpg fingerprint
  [ "$(wc -w <<<"$gpg_fps")" -eq "$(wc -w <<<"$rpm_ids")" ] ||
    refuse "gpg and rpm see different keys at $KEY_URL (gpg: [${gpg_fps//$'\n'/ }], rpm: [${rpm_ids//$'\n'/ }])"
  for id in $rpm_ids; do
    match=""
    for fp in $gpg_fps; do
      if [ "${fp: -${#id}}" = "$id" ]; then match=$fp; fi
    done
    [ -n "$match" ] || refuse "rpm sees key $id at $KEY_URL that gpg does not"
  done
  for fp in $gpg_fps; do
    case "$pins" in
      *" $fp "*) ;;
      *) refuse "the key at $KEY_URL has primary fingerprint(s) [${gpg_fps//$'\n'/ }]; every one must be pinned in" \
        "$PIN_VAR. Nothing was imported." ;;
    esac
  done
  VERIFIED=$gpg_fps
}

# all_imported FPS: each key is already in the system rpm keyring (full fingerprint or 8-hex key ID)
all_imported() {
  local have fp
  have=$(rpm -q gpg-pubkey --qf '%{VERSION}\n' 2>/dev/null | tr 'a-f' 'A-F') || true
  for fp in $1; do
    grep -qxF -e "$fp" -e "${fp: -8}" <<<"$have" || return 1
  done
}

# Without DEST: skip the network fetch when the pinned keys are imported, so an offline rerun does not fail
if [ -z "$DEST" ] && all_imported "$pins"; then exit 0; fi
# With DEST: fetch on every run, so a vendor's new signing subkeys (Google rotates them under the same primary key)
# reach the keyring; offline, a DEST that still checks out and is imported is kept
have_dest=0
if [ -n "$DEST" ] && [ -f "$DEST" ] && (verify "$DEST" && all_imported "$VERIFIED") 2>/dev/null; then have_dest=1; fi
if ! curl --proto '=https' --tlsv1.2 -fsSL "$KEY_URL" -o "$WORKDIR/key.asc"; then
  if [ "$have_dest" = 1 ]; then
    echo "WARNING: could not fetch $KEY_URL; keeping the checked key in $DEST" >&2
    exit 0
  fi
  exit 1
fi
verify "$WORKDIR/key.asc"
if [ -n "$DEST" ]; then
  # sha256sum (coreutils, in every image) rather than cmp (diffutils, absent from minimal ones: every run would re-import)
  if [ "$have_dest" = 1 ] && [ "$(sha256sum < "$WORKDIR/key.asc")" = "$(sha256sum < "$DEST")" ]; then exit 0; fi
  # rpm 6 merges new subkeys into an installed key on import; older rpm (4.16 on EL9, 4.19 on EL10) silently keeps the
  # installed copy, so a rotated signing subkey would never land. There, remove the installed copy of exactly these
  # primary keys (by their own gpg-pubkey version: the full fingerprint or its last 8 hex digits) before importing.
  rpm_major=$(rpm --version 2>/dev/null | sed -n 's/^RPM version \([0-9][0-9]*\)\..*/\1/p')
  if [ -n "$rpm_major" ] && [ "$rpm_major" -lt 6 ]; then
    installed=$(rpm -q gpg-pubkey --qf '%{VERSION}-%{RELEASE}\n' 2>/dev/null) || true
    for fp in $VERIFIED; do
      short=$(printf '%s' "${fp: -8}" | tr 'A-F' 'a-f')
      full=$(printf '%s' "$fp" | tr 'A-F' 'a-f')
      while IFS= read -r nvr; do
        case "${nvr%-*}" in
          "$short" | "$full") rpm -e "gpg-pubkey-$nvr" ;;
        esac
      done <<<"$installed"
    done
  fi
  rpm --import "$WORKDIR/key.asc"
  install -m 0644 "$WORKDIR/key.asc" "$DEST"  # only after rpm accepted it
else
  rpm --import "$WORKDIR/key.asc"
fi
