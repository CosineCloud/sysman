#!/bin/sh
set -eu
if [ ! -f /certs/server.key ]; then
  umask 077
  openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 365 \
    -keyout /certs/server.key -out /certs/server.crt \
    -subj '/CN=SysMan demo agent' \
    -addext 'subjectAltName=IP:172.30.86.3,IP:127.0.0.1' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign'
fi
cat /etc/ssl/certs/ca-certificates.crt /certs/server.crt > /certs/ca-bundle.crt
chmod 600 /certs/server.key
