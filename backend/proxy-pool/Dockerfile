# 3proxy image for Litetube.
#
# Image: litetube-3proxy:latest
# Built once via `docker compose build`. Doesn't change often — the FastAPI
# container regenerates 3proxy.cfg and SIGHUPs the parent process on user
# signup/ban/revoke. No rebuild needed for routine operations.

FROM alpine:3.19 AS builder

RUN apk add --no-cache gcc musl-dev make linux-headers git openssl-dev
RUN git clone --depth 1 https://github.com/3proxy/3proxy.git /src
WORKDIR /src
RUN make -f Makefile.Linux

FROM alpine:3.19

RUN apk add --no-cache \
        supervisor \
        curl \
        tini \
        bash

COPY --from=builder /src/bin/3proxy /usr/bin/3proxy

# Drop privileges by default. supervisord starts the 3proxy program as
# 'nobody'. The bind shell is still root so it can read configuration.
RUN addgroup -S litetube 2>/dev/null || true && \
    adduser -S -G litetube litetube 2>/dev/null || true

# Directories
RUN mkdir -p /var/lib/3proxy /var/log/3proxy /var/log/supervisor /etc/supervisor.d \
 && chown -R litetube:litetube /var/lib/3proxy /var/log/3proxy

# Configuration files
COPY supervisord.conf        /etc/supervisord.conf
COPY 3proxy.cfg.bootstrap    /etc/3proxy.cfg.bootstrap
COPY entrypoint.sh           /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh && \
    chmod 0644 /etc/3proxy.cfg.bootstrap && \
    chown litetube:litetube /etc/3proxy.cfg.bootstrap /usr/local/bin/entrypoint.sh

VOLUME ["/var/lib/3proxy", "/var/log/3proxy"]

# entrypoint.sh: generates a fresh random placeholder password BEFORE
# supervisord launches 3proxy, so the literal in git never matches what's
# actually loaded.  See entrypoint.sh comments for rationale.
ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD []



