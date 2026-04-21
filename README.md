# Fuckdl

PlayReady and Widevine DRM downloader and decrypter with multi-VPN proxy support.

## Git LFS Required

This repository uses Git LFS for large assets in `binaries/`, `Tools/`, `assets/`, and some files under `fuckdl/devices/`.

Before cloning or pulling large-file updates, install Git LFS and initialize it:

```powershell
git lfs install
git clone https://github.com/chromedecrypt/Fuckdl.git
```

If you already cloned the repo before installing Git LFS, run:

```powershell
git lfs install
git lfs pull
```

## Quick Start

1. Install Python 3.10 to 3.12 and add it to `PATH`.
2. Install the Microsoft Visual C++ Redistributable:
   `https://aka.ms/vs/17/release/vc_redist.x64.exe`
3. Run `install.bat`.
4. See `How.to.use.txt` for service-specific cookie and credential setup.
