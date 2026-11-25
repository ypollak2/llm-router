#!/bin/sh
# make-ca.sh: create a name-constrained CA + leaf for api.anthropic.com.
#   CA_DIR  = $FWDPROXY_CA_DIR or $HOME/.llm-router/fwd-ca   (0700, files 0600)
#     ca.crt, leaf.crt, leaf.key, leaf.cnf, leaf.csr      <- fwdproxy reads only leaf.crt/leaf.key
#   KEY_DIR = $FWDPROXY_CA_KEY_DIR or "<CA_DIR>-key"        (0700, files 0600), kept OFFLINE
#     ca.key, ca.cnf, ca.srl                              <- used only to sign the leaf
# CA: RSA 2048, 825 days, CA:TRUE pathlen:0, critical nameConstraints permitted DNS:api.anthropic.com.
# Leaf: SAN api.anthropic.com, serverAuth, 397 days.
# Usage: make-ca.sh [--force | --renew-leaf]
#   (default)     refuses to overwrite an existing CA; --force replaces CA and leaf
#   --renew-leaf  re-issues ONLY the leaf from the existing CA (leaf expires after 397 days)
# Never touches the macOS keychain.
set -eu

FORCE=0
RENEW=0
for a in "$@"; do
  case $a in
    --force) FORCE=1 ;;
    --renew-leaf) RENEW=1 ;;
    *) echo "usage: make-ca.sh [--force | --renew-leaf]" >&2; exit 2 ;;
  esac
done
if [ "$FORCE" -eq 1 ] && [ "$RENEW" -eq 1 ]; then
  echo "make-ca: --force and --renew-leaf are exclusive" >&2
  exit 2
fi

CA_DIR=${FWDPROXY_CA_DIR:-$HOME/.llm-router/fwd-ca}
KEY_DIR=${FWDPROXY_CA_KEY_DIR:-${CA_DIR}-key}
OPENSSL=${OPENSSL:-openssl}

umask 077

issue_leaf() {
  cat > "$CA_DIR/leaf.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = api.anthropic.com
[v3_leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = DNS:api.anthropic.com
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
EOF
  "$OPENSSL" genrsa -out "$CA_DIR/leaf.key" 2048 2>/dev/null
  "$OPENSSL" req -new -key "$CA_DIR/leaf.key" -sha256 -config "$CA_DIR/leaf.cnf" -out "$CA_DIR/leaf.csr"
  "$OPENSSL" x509 -req -in "$CA_DIR/leaf.csr" -CA "$CA_DIR/ca.crt" -CAkey "$KEY_DIR/ca.key" \
    -CAcreateserial -CAserial "$KEY_DIR/ca.srl" -sha256 -days 397 \
    -extfile "$CA_DIR/leaf.cnf" -extensions v3_leaf -out "$CA_DIR/leaf.crt" 2>/dev/null
  for f in leaf.cnf leaf.key leaf.csr leaf.crt; do chmod 600 "$CA_DIR/$f"; done
  "$OPENSSL" verify -CAfile "$CA_DIR/ca.crt" "$CA_DIR/leaf.crt" >/dev/null
}

if [ "$RENEW" -eq 1 ]; then
  if [ ! -f "$KEY_DIR/ca.key" ] || [ ! -f "$CA_DIR/ca.crt" ]; then
    echo "make-ca: no CA to renew from ($KEY_DIR/ca.key and $CA_DIR/ca.crt needed)" >&2
    exit 1
  fi
  issue_leaf
  echo "make-ca: re-issued leaf only in $CA_DIR (CA unchanged, keychain untouched)"
  exit 0
fi

if [ -e "$CA_DIR/ca.crt" ] || [ -e "$KEY_DIR/ca.key" ] || [ -e "$CA_DIR/ca.key" ]; then
  if [ "$FORCE" -ne 1 ]; then
    echo "make-ca: CA already exists ($CA_DIR / $KEY_DIR); refusing to overwrite (use --force)" >&2
    exit 1
  fi
fi

mkdir -p "$CA_DIR" "$KEY_DIR"
chmod 700 "$CA_DIR" "$KEY_DIR"

cat > "$KEY_DIR/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = llm-router fwdproxy CA
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
nameConstraints = critical,permitted;DNS:api.anthropic.com
EOF

"$OPENSSL" genrsa -out "$KEY_DIR/ca.key" 2048 2>/dev/null
"$OPENSSL" req -x509 -new -key "$KEY_DIR/ca.key" -sha256 -days 825 \
  -config "$KEY_DIR/ca.cnf" -out "$CA_DIR/ca.crt"
chmod 600 "$KEY_DIR/ca.key" "$KEY_DIR/ca.cnf" "$CA_DIR/ca.crt"
issue_leaf
[ -f "$KEY_DIR/ca.srl" ] && chmod 600 "$KEY_DIR/ca.srl"
echo "make-ca: wrote CA + leaf in $CA_DIR, CA key in $KEY_DIR (keychain untouched)"
