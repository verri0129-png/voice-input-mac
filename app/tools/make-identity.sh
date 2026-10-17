#!/bin/bash
# Create a STABLE self-signed code-signing identity for VoiceInputMac.
#
# Why this exists
# ---------------
# An ad-hoc signature (`codesign -s -`) has a designated requirement that is just the
# binary's cdhash. Every rebuild changes it, so macOS/TCC sees "a different app" and
# re-asks for Microphone / Accessibility — the "一直跳權限通知" symptom. Signing with a
# fixed certificate makes the designated requirement `identifier "..." and certificate
# leaf = H"<固定>"`, which is constant across rebuilds, so a one-time grant sticks.
#
#   ./make-identity.sh            create (or recreate) the identity
#   ./make-identity.sh --delete   remove the keychain and the identity
#
# The identity lives in its OWN keychain with an EMPTY password (CI-style) so codesign
# never needs the login password and never pops a keychain access dialog. The login
# keychain is left untouched; we only append ours to the user keychain search list.
set -euo pipefail

NAME="VoiceInputMac Local Signing"
KC="$HOME/Library/Keychains/voice-input-mac-signing.keychain-db"

if [ "${1:-}" = "--delete" ]; then
  CUR="$(security list-keychains -d user | sed 's/[" ]//g' | tr '\n' ' ')"
  KEEP=""
  for k in $CUR; do [ "$k" = "$KC" ] || KEEP="$KEEP $k"; done
  [ -n "${KEEP// /}" ] && security list-keychains -d user -s $KEEP || true
  security delete-keychain "$KC" 2>/dev/null || true
  echo "[identity] deleted $KC（已從 search list 移除）"
  exit 0
fi

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/openssl.cnf" <<EOF
[ req ]
distinguished_name = dn
x509_extensions = ext
prompt = no
[ dn ]
CN = $NAME
[ ext ]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
EOF
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
  -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/openssl.cnf" 2>/dev/null
# macOS `security import` cannot read OpenSSL 3's default PBES2/AES p12
# ("MAC verification failed during PKCS12 import") — export it in legacy 3DES/RC2 form.
openssl pkcs12 -export -legacy -inkey "$TMP/key.pem" -in "$TMP/cert.pem" -name "$NAME" \
  -out "$TMP/id.p12" -passout pass:voiceinput 2>/dev/null

security delete-keychain "$KC" 2>/dev/null || true
security create-keychain -p "" "$KC"
security set-keychain-settings "$KC"           # 不自動上鎖（無人值守的 build 才不會卡）
security unlock-keychain -p "" "$KC"
security import "$TMP/id.p12" -k "$KC" -P voiceinput -T /usr/bin/codesign -T /usr/bin/security
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "" "$KC" >/dev/null

CUR="$(security list-keychains -d user | sed 's/[" ]//g' | tr '\n' ' ')"
case " $CUR " in
  *" $KC "*) ;;
  *) security list-keychains -d user -s $CUR "$KC" ;;
esac

echo "[identity] keychain: $KC"
echo "[identity] find-identity -p codesigning（-v 只列受信任的；自簽不會出現）:"
security find-identity -p codesigning "$KC" | sed 's/^/  /'
