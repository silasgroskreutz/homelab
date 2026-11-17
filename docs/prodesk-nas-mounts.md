# Prodesk node fstab — NAS CIFS mounts

Prodesk (192.168.0.101, k3s node) mounts the TOS NAS at `192.168.0.2`.
This is node-level state, NOT managed by Flux. It lives in the node's
`/etc/fstab` and is not in this repo.

## The mounts

```
//192.168.0.2/public/Books /mnt/nas-books cifs user=AI_bot,pass=<pw>,vers=3.0,_netdev,x-systemd.automount,nofail 0 0
//192.168.0.2/public/Music /mnt/nas/music cifs credentials=/root/.smbcred_nas,vers=3.0,_netdev,x-systemd.automount,nofail 0 0
//192.168.0.2/public/Books /mnt/nas/books cifs credentials=/root/.smbcred_nas,vers=3.0,_netdev,x-systemd.automount,nofail 0 0
//192.168.0.2/public /mnt/nas-roms cifs guest,uid=1000,gid=0,forceuid,file_mode=0755,dir_mode=0755,_netdev,x-systemd.automount,nofail 0 0
```

`/mnt/nas-roms` must be the **entire** `public` share. `Books`, `Backups`,
`Music` and `Movies` are siblings of `roms` inside `public`, not children of
it. BookOrbit mounts `/mnt/nas-roms/Books` and the backup CronJobs write to
`/mnt/nas-roms/Backups`; both break if `/mnt/nas-roms` points at
`public/roms`.

`uid=1000,forceuid` on `/mnt/nas-roms` is what lets the BookOrbit container
(written as uid 1000) write into the share. The other three are read-mostly
and stay uid 0.

## Why `_netdev,x-systemd.automount,nofail`

Prodesk boots faster than the NAS answers. With a plain fstab mount the boot
loses the race and every CIFS mount fails:

```
CIFS: VFS: Error connecting to socket. Aborting operation. (rc=-101)
```

`x-systemd.automount` defers the mount to first access, by which time the
network is up. `nofail` stops a dead NAS from blocking the boot.

## Recovery after a node reboot

```bash
mount | grep nas                                  # what's actually mounted
sudo /usr/bin/mount -a                            # plain `mount -a` may not be on PATH
ls -d /mnt/nas-roms/Books /mnt/nas-roms/Backups    # share ROOT must resolve
stat -c '%u %g %a %n' /mnt/nas-roms/Books          # expect 1000 0 755
```

If `/mnt/nas-roms/Books` is missing, the mount source is wrong (`public/roms`
instead of `public`) — fix the fstab line, not the pod.

Stale pods after a node reboot show STATUS `Unknown`, which the
`status.phase!=Running` field selector does not catch. Delete them by name:

```bash
sudo /usr/local/bin/k3s kubectl -n <ns> delete pod <pod> --grace-period=0 --force
```

## Diagnosing what was mounted before a reboot

`/home/silas/sweep.log` on Prodesk records the previous boot's full mount
table. It is the fastest way to see the mount configuration that actually
worked, rather than what someone intended to configure.

## Verify the backup pipeline end to end

The BookOrbit backup CronJob exercises the write path through the mount:

```bash
sudo /usr/local/bin/k3s kubectl -n backup create job \
  --from=cronjob/bookorbit-backup verify-$(date +%s)
sudo /usr/local/bin/k3s kubectl -n backup logs job/verify-<ts> --tail=14
```

Expect `published /backups/Backups/bookorbit/...` and a matching sha256, then
confirm the artifact exists at `/mnt/nas-roms/Backups/bookorbit/`.
