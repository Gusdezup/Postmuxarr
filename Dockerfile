FROM python:3.12-alpine
ARG BDINFO_VERSION=0.4.2
RUN apk add --no-cache mkvtoolnix mediainfo 7zip \
 && (command -v 7z || ln -s "$(command -v 7zz)" /usr/local/bin/7z) \
 && pip install --no-cache-dir fastapi uvicorn pyyaml \
 && wget -qO- "https://github.com/autobrr/go-bdinfo/releases/download/v${BDINFO_VERSION}/bdinfo_${BDINFO_VERSION}_linux_amd64.tar.gz" \
    | tar xz -C /usr/local/bin bdinfo \
 && bdinfo version && mkvmerge --version && 7z | head -2
RUN apk upgrade --no-cache ca-certificates \
 && rm -f /usr/share/ca-certificates/mozilla/emSign* \
 && (update-ca-certificates || true) \
 && python3 -c "import ssl,sys; n=[c for c in ssl.create_default_context().get_ca_certs() if \"emSign\" in str(c)]; sys.exit(\"emSign encore present : %d\" % len(n) if n else 0)"
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app/ /app/
EXPOSE 8790
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8790"]
