# syntax=docker/dockerfile:1.7
# Test on the builder's CPU, then cross-compile the binaries for the target.
FROM --platform=$BUILDPLATFORM golang:1.27-alpine@sha256:8a5910f31396cd4d89662f56c68b3ae31d374308270a1c3bd96672ee5ed43414 AS build
WORKDIR /src
RUN apk add --no-cache patch
ADD --checksum=sha256:eb914e9a31f47dd1ee6ae5edcfe709ef2e1a2b342c313ec11ff832562fdee10c https://codeload.github.com/Altinity/clickhouse-backup/tar.gz/824f1ad5ca669fb9df7e5082f4bed6554fcf678d /tmp/source.tar.gz
RUN tar -xzf /tmp/source.tar.gz --strip-components=1 && rm /tmp/source.tar.gz
COPY api-security.patch /tmp/api-security.patch
RUN patch -p1 < /tmp/api-security.patch
COPY check-backup-config.go /src/cmd/check-backup-config/main.go
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go test ./pkg/server ./pkg/config -run '^(TestBackupAPIAuthDoesNotLogCredentials|TestBackupAPIRejectsEmptyReloadCredentials|TestBackupServerCredentials|TestBackupConfiguredTimeout|TestValidateConfigErrors)$' -count=1
ARG TARGETARCH
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    GOOS=linux GOARCH=$TARGETARCH go build -trimpath -buildvcs=false \
    -ldflags='-s -w -X main.version=2.8.1-mc.1 -X main.gitCommit=824f1ad5ca669fb9df7e5082f4bed6554fcf678d -X main.buildDate=2026-09-30' \
    -o /out/clickhouse-backup ./cmd/clickhouse-backup && \
    GOOS=linux GOARCH=$TARGETARCH go build -trimpath -buildvcs=false -ldflags='-s -w' \
    -o /out/check-backup-config ./cmd/check-backup-config

# Preserve the upstream utilities, certificates, user IDs, and general CLI.
FROM altinity/clickhouse-backup:2.8.1@sha256:08016b048f7e6035c048501315c2a788e5a782f15f168e042c7bd48d5a388cc4
COPY --from=build /out/clickhouse-backup /bin/clickhouse-backup
COPY --from=build /out/check-backup-config /usr/local/bin/check-backup-config
COPY --chmod=0755 start-backup.sh /usr/local/bin/start-backup.sh
LABEL org.opencontainers.image.version="2.8.1-mc.1" \
      org.opencontainers.image.source="https://github.com/monte-carlo-data/helm-ao-data-platform/tree/main/images/clickhouse-backup"
