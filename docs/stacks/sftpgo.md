---
title: "SFTPGo"
---

## SFTPGo

![SFTPGo](https://img.shields.io/badge/SFTPGo-2D3748?logo=files&logoColor=white)

**SFTP/SCP server with R2 backend — file-protocol front door onto the datalake**

SFTPGo is a fully-featured SFTP/SCP/WebDAV/FTPS server with a web admin UI, per-user virtual filesystems, and pluggable backends. **In this Nexus-Stack config the default exposed surface is SFTP plus the web admin UI; WebDAV and FTPS are supported upstream but disabled by default in the shipped compose** (set `SFTPGO_WEBDAVD__BINDINGS__0__PORT` to a real port in `stacks/sftpgo/docker-compose.yml` to enable WebDAV). Configured with the R2 datalake bucket as its S3 backend, SFTPGo lets external tools read and write the same lake the rest of the stack operates on, using only standard SFTP clients — no S3 SDK required. Common use cases:

- Partner-data dropoffs that arrive over plain SFTP
- BI tools or legacy ETLs that can only push files via SFTP
- Hand-shipping a CSV from a laptop into the lake without configuring an S3 client
- Reading files written by Kestra flows from a remote workstation

| Setting | Value |
|---------|-------|
| Default Port (web UI) | `8090` |
| SFTP Port | `2022` |
| Suggested Subdomain | `sftpgo` |
| Public Access | No (Cloudflare Access on the web UI; SFTP behind opt-in firewall rule) |
| Authentication | Web admin: SFTPGo username/password (auto-configured, gated by Cloudflare Access on top); SFTP user: password (auto-configured) |
| Website | [sftpgo.com](https://sftpgo.com) |
| Source | [GitHub](https://github.com/drakkan/sftpgo) |

> ✅ **Auto-configured:** Admin account `nexus-sftpgo` and a default SFTP user `nexus-default` (with R2 vfs) are created automatically during deployment. Credentials available in Infisical under folder `sftpgo`, keys `SFTPGO_ADMIN_PASSWORD` and `SFTPGO_USER_PASSWORD`.

### How to access

**Web admin UI** (Cloudflare Access OTP on top of SFTPGo's own username/password login):
```
https://sftpgo.<your-domain>
```
Log in with `nexus-sftpgo` and the password from Infisical (folder `sftpgo`, key `SFTPGO_ADMIN_PASSWORD`).

**SFTP**: the SFTP port (`2022`) is closed by default — SFTPGo is reachable only from inside the Docker network until you opt in. Open it via **Firewall** in the Control Plane (toggle `sftpgo` → `sftp`, restrict to your source IP or range, hit **Spin Up**). Then:
```
sftp -P 2022 nexus-default@<your-server-ip>
```
Password from Infisical (folder `sftpgo`, key `SFTPGO_USER_PASSWORD`).

### R2 backend

The default user `nexus-default` has its virtual filesystem mapped to the R2 datalake bucket with key prefix `sftp/nexus-default/`. So:

```
sftp> put report.csv
```
…lands in R2 at `s3://<your-bucket>/sftp/nexus-default/report.csv`. You can read it back with any tool that talks S3:

- The DuckDB query in the seeded `r2-taxi-pipeline` Kestra flow
- `aws s3 cp` (with R2 endpoint override)
- The s3manager UI at `https://s3manager.<your-domain>`

The reverse direction works too: a file written via S3 to the same key prefix shows up under SFTP as soon as the next `ls` runs.

### Adding more SFTP users

In the web admin UI: **Users → Add**. Pick `Cloud filesystem (S3)` for the filesystem provider and reuse the same R2 endpoint + access key from Infisical. Use a distinct `key_prefix` (e.g. `sftp/<username>/`) so each user gets an isolated home directory under the same bucket.
