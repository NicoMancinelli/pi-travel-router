# pi-travel-router image build

This directory contains the [pi-gen](https://github.com/RPi-Distro/pi-gen) custom stage and config used to produce a flashable SD card image for the pi-travel-router project. Builds run in GitHub Actions (`.github/workflows/build-image.yml`).

## What the build produces

A single compressed image: `deploy/<date>-travelrouter-arm64-lite.img.xz` (Raspberry Pi OS Lite, Bookworm, 64-bit). On first boot the device runs the firstboot wizard, which collects setup answers via a captive-portal-style web form and then runs `install.sh` non-interactively.

Pre-installed in the image:

- The `pi-travel-router` repo at `/opt/pi-travel-router` (cloned at the build's git SHA)
- A `firstboot.service` unit, enabled
- `git`, `curl`, `ca-certificates`, `avahi-daemon`
- Hostname set to `travelrouter`
- Image metadata at `/etc/travel-router-image-version`

`install.sh` is **not** run during the image build. The wizard runs it on first boot after collecting user input.

## Flashing

Use Raspberry Pi Imager (recommended):

1. Download the `.img.xz` from the latest GitHub Release.
2. In Raspberry Pi Imager click **Choose OS** -> **Use custom**, pick the `.img.xz`.
3. **Choose Storage** -> select your SD card.
4. Click **Write**. Imager handles `xz` decompression for you.

Manual flash on Linux/macOS:

```sh
xz -d travelrouter-*.img.xz
sudo dd if=travelrouter-*.img of=/dev/diskN bs=4M status=progress conv=fsync
```

Replace `/dev/diskN` with your SD card device. Use `lsblk` (Linux) or `diskutil list` (macOS) first to confirm.

## Default credentials

- Username: `root`
- Password: `changeme`

`root` is the only login account on the image; SSH password login is enabled for `root` so the wizard can be reached without prior key setup. **Change the password immediately after first boot** with:

```sh
ssh root@travelrouter.local   # password: changeme
passwd
```

Once you provide an SSH public key in the firstboot wizard, password authentication is automatically disabled by `install.sh` and only key auth remains.

## Triggering a build

Two ways:

1. **Tag-driven release** -- push a tag matching `v*` (e.g. `v0.3.0`). The workflow builds and attaches the `.img.xz` to a GitHub Release.
2. **Manual run** -- in GitHub -> Actions -> "Build Pi Image" -> Run workflow. The image is uploaded as a workflow artifact (retained 14 days).

Each build embeds the triggering commit SHA into `/opt/pi-travel-router` and `/etc/travel-router-image-version` on the image.

## Local testing

pi-gen requires Linux with binfmt + qemu-user-static. macOS is not supported for native pi-gen runs (Docker workarounds exist but are slow and finicky). On a Linux host:

```sh
git clone https://github.com/RPi-Distro/pi-gen
cd pi-gen
sudo apt install -y coreutils quilt parted qemu-user-static debootstrap zerofree zip \
    dosfstools libarchive-tools libcap2-bin grep rsync xz-utils file git curl bc \
    qemu-utils kpartx gpg pigz arch-test
cp /path/to/pi-travel-router/build/config ./config
ln -s /path/to/pi-travel-router/build/stage-travel-router ./stage-travel-router
touch stage2/SKIP_IMAGES
REPO_URL=https://github.com/NicoMancinelli/pi-travel-router.git GIT_REF=main \
    sudo -E ./build.sh
```

Output lands in `pi-gen/deploy/`.

## Update path

After first boot + wizard, the device is checked out on `main` at `/opt/pi-travel-router`. `scripts/update-router.sh` keeps the running system current via `git pull` plus targeted re-runs of install steps. New images are only needed for major OS version bumps (e.g. Bookworm -> Trixie) or to refresh the bootstrap baseline.
