# Backup engine image.
#
# The engine is a Python 3 script. pgvector/pgvector:pg18 is the base
# because it already carries the pieces the shared engine needs for its
# other kinds: pg_dump for "pgdump" apps and tar for "tar" apps. One image
# for every app keeps the operator story simple.
#
# The base image ships NO python3, so it is installed here, at build time.
# The pod runs as UID 1000 and cannot install anything at runtime.
#
# Build and push (from a machine that can reach the internal registry):
#   docker build -t 192.168.0.101:5000/homelab-backup:1.0.0 tools/backup
#   docker push 192.168.0.101:5000/homelab-backup:1.0.0
FROM pgvector/pgvector:pg18

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

# UID 1000 / GID 0 is required by the CIFS share: /mnt/nas-roms is mounted
# uid=1000,forceuid,gid=0,noforcegid,file_mode=0755,dir_mode=0755, so any
# other UID cannot create files there. Root would create files that UID
# 1000 can then neither overwrite nor delete.
USER 1000:0

WORKDIR /scripts

# The engine and its JSON config are mounted from a ConfigMap at /scripts,
# not baked into this image.
ENTRYPOINT ["python3", "/scripts/backup.py"]
CMD ["--config", "/scripts/config.json"]
