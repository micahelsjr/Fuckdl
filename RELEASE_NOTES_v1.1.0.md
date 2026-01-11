# FuckDL v1.1.0 Release Notes

## Release v1.1.0

This release includes important bug fixes and improvements.

## What's New in v1.1.0

### Bug Fixes
- 🔧 **Fixed**: AttributeError when downloader config is missing
- ✅ Added default downloader configuration (n_m3u8dl-re)
- 🛡️ Improved error handling for missing config attributes

### Amazon Prime Video Improvements
- 🔧 **Fixed**: Amazon Prime Video now uses new endpoints for better reliability
- 🎬 **New**: Full support for 4K ISM (Smooth Streaming) downloads
- ✨ Improved download stability and error handling
- 🚀 Enhanced compatibility with Amazon Prime Video's streaming infrastructure

### 4K ISM Download Support
- Support for downloading 4K content via ISM manifest format
- Enhanced compatibility with Amazon Prime Video's streaming infrastructure
- Better handling of high-quality video streams
- Improved manifest parsing for ISM format

## Features

- ✅ Support for 30+ streaming services
- ✅ Playready and Widevine DRM decryption
- ✅ Multiple downloader support (N_m3u8DL-RE, Aria2c, saldl)
- ✅ CDM device support (SL2000/SL3000, WVD files)
- ✅ Comprehensive command-line interface
- ✅ Multiple quality and codec options
- ✅ Subtitle and audio track selection
- ✅ Episode range selection
- ✅ Proxy support
- ✅ Key vault integration
- ✅ **Amazon Prime Video fixed with new endpoints**
- ✅ **Support for 4K ISM downloads**

## Supported Services

- All4
- Amazon Prime Video
- Apple TV Plus
- BBC iPlayer
- BritBox
- Crave
- Disney Plus
- Discovery Plus
- HBO Max
- Hulu
- iTunes
- ITV
- Movies Anywhere
- MY5
- Netflix
- Now TV (IT/UK)
- Paramount Plus
- Peacock
- Pluto TV
- Rakuten TV
- Roku
- Skyshowtime
- Stan
- TUBI
- Videoland
- WowTV

## Installation

```bash
poetry install
```

## Quick Start

```bash
# Get help
poetry run fuckdl dl --help

# Download from Amazon Prime Video (now with 4K ISM support)
poetry run fuckdl dl -al en -sl en -q 2160 Amazon https://www.primevideo.com/...

# Download 4K HDR from Amazon
poetry run fuckdl dl -al en -sl en -q 2160 -r HDR -v H265 Amazon https://www.primevideo.com/...
```

## Documentation

See `HOW_TO_USE.md` for complete usage guide with all command examples.

## CDM Devices Included

- Genius Fashion GAE TV Smart TV (SL3000)
- Hisense SmartTV HU32E5600FHWV (SL3000)
- Xiaomi Mi A1 (WVD)

## Created By

**Barbie DRM**  
https://t.me/barbiedrm

## Repository

https://github.com/chromedecrypt/Fuckdl
