import base64
import json
import os.path
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from hashlib import md5

import click
import httpx
import isodate
import requests
import xmltodict
from langcodes import Language

from fuckdl.objects import TextTrack, Title, Tracks, VideoTrack
from fuckdl.objects.tracks import AudioTrack, MenuTrack
from fuckdl.services.BaseService import BaseService
from fuckdl.utils import is_close_match, short_hash, try_get
from fuckdl.utils.widevine.device import LocalDevice


class HBOMax(BaseService):
    """
    Service code for HBO Max streaming service (https://play.hbomax.com).

    \b
    Authorization: Cookies
    Security: UHD@L1 FHD@L1 HD@L3
    """

    ALIASES = ["HBOMAX", "hbomax"]

    TITLE_RE = [
        r"^(?:https?:\/\/(?:www\.|play\.)?(?:max|hbomax)\.com\/)?(?P<type>[^/]+)/(?:[^/]+/)?(?P<id>[^/]+)",
    ]

    VIDEO_CODEC_MAP = {
        "H264": ["avc1"],
        "H265": ["hvc1", "dvh1"]
    }

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="HBOMax", short_help="https://play.hbomax.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return HBOMax(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.title = self.parse_title(ctx, title)

        self.cdm = ctx.obj.cdm

        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.alang = ctx.parent.params["alang"]
        self.quality = ctx.parent.params["quality"] or 1080
        if self.range == 'HDR10':
            self.vcodec = "H265"
        
        self.configure()


    def get_titles(self):
        content_type = self.title['type']
        external_id = self.title['id']
        
        response = self.session.get(
            f"https://default.prd.api.hbomax.com/cms/routes/{content_type}/{external_id}?include=default",
        )

        try:
            content_data = [x for x in response.json()["included"] if "attributes" in x and "title" in 
                               x["attributes"] and x["attributes"]["alias"] == "generic-%s-blueprint-page" % (re.sub(r"-", "", content_type))][0]["attributes"]
            content_title = content_data["title"]
        except:
            content_data = [x for x in response.json()["included"] if "attributes" in x and "alternateId" in 
                               x["attributes"] and x["attributes"]["alternateId"] == external_id and x["attributes"].get("originalName")][0]["attributes"]
            content_title = content_data["originalName"]

        if content_type == "sport" or content_type =="event":
            included_dt = response.json()["included"]

            for included in included_dt:
                for key, data in included.items():
                    if key == "attributes":
                        for k,d in data.items():
                            if d == "VOD":
                                event_data = included

            release_date = event_data["attributes"].get("airDate") or event_data["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year

            return Title(
                id_=external_id,
                type_=Title.Types.MOVIE,
                name=content_title.title(),
                year=year,
                # original_lang=,
                source=self.ALIASES[0],
                service_data=event_data,
            )

        if content_type == "movie" or content_type == "standalone":
            metadata = self.session.get(
                url=f"https://default.prd.api.hbomax.com/content/videos/{external_id}/activeVideoForShow?&include=edit"
            ).json()['data']

            try:
                edit_id = metadata['relationships']['edit']['data']['id']
            except:
                for x in response.json()["included"]:
                    if x.get("type") == "video" and x.get("relationships", {}).get("show", {}).get("data", {}).get("id") == external_id:
                        metadata = x

            release_date = metadata["attributes"].get("airDate") or metadata["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            return Title(
                id_=external_id,
                type_=Title.Types.MOVIE,
                name=content_title,
                year=year,
                # original_lang=,
                source=self.ALIASES[0],
                service_data=metadata,
            )

        if content_type in ["show", "mini-series", "topical"]:
            episodes = []
            if content_type == "mini-series":
                alias = "generic-miniseries-page-rail-episodes"
            elif content_type == "topical":
                alias = "generic-topical-show-page-rail-episodes"
            else:
                alias = "-%s-page-rail-episodes-tabbed-content" % (content_type)

            included_dt = response.json()["included"]
            season_data = [data for included in included_dt for key, data in included.items()
                           if key == "attributes" for k,d in data.items() if alias in str(d).lower()][0]
            season_data = season_data["component"]["filters"][0]
            
            seasons = [int(season["value"]) for season in season_data["options"]]
            
            season_parameters = [(int(season["value"]), season["parameter"]) for season in season_data["options"]
                for season_number in seasons if int(season["value"]) == int(season_number)]
            if not season_parameters:
                raise self.log.exit("season(s) %s not found")

            for (value, parameter) in season_parameters:
                data = self.session.get(url="https://default.prd.api.hbomax.com/cms/collections/generic-show-page-rail-episodes-tabbed-content?include=default&pf[show.id]=%s&%s" % (external_id, parameter)).json()
                #PARA MAS DE 100 EPISODIOS &page[items.number]=2
                try:
                    episodes_dt = sorted([dt for dt in data["included"] if "attributes" in dt and "videoType" in 
                                    dt["attributes"] and dt["attributes"]["videoType"] == "EPISODE" 
                                    and int(dt["attributes"]["seasonNumber"]) == int(parameter.split("=")[-1])], key=lambda x: x["attributes"]["episodeNumber"])
                except KeyError:
                    raise self.log.exit("season episodes were not found")
                
                episodes.extend(episodes_dt)
             
            titles = []
            release_date = episodes[0]["attributes"].get("airDate") or episodes[0]["attributes"].get("firstAvailableDate")
            year = datetime.strptime(release_date, '%Y-%m-%dT%H:%M:%SZ').year
            
            season_map = {int(item[1].split("=")[-1]): item[0] for item in season_parameters}

            for episode in episodes:
                titles.append(
                    Title(
                        id_=episode['id'],
                        type_=Title.Types.TV,
                        name=content_title,
                        year=year,
                        season=season_map.get(episode['attributes'].get('seasonNumber')),
                        episode=episode['attributes']['episodeNumber'],
                        episode_name=episode['attributes']['name'],
                        # original_lang=edit.get('originalAudioLanguage'),
                        source=self.ALIASES[0],
                        service_data=episode
                    )
                )

            return titles

    def get_tracks(self, title: Title):
        edit_id = title.service_data['relationships']['edit']['data']['id']
        
        response = self.session.post(
            url=self.config['endpoints']['playbackInfo'],
            json={
                "appBundle": "com.wbd.stream",
                "applicationSessionId": str(uuid.uuid4()),
                "capabilities": {
                    "codecs": {
                        "audio": {
                            "decoders": [
                                {
                                    "codec": "eac3",
                                    "profiles": [
                                        "lc",
                                        "he",
                                        "hev2",
                                        "xhe",
                                        'atmos',
                                    ]
                                },
                                {
                                    'codec': 'ac3',
                                    'profiles': []
                                }
                            ]
                        },
                        "video": {
                            "decoders": [
                                {
                                    "codec": "h264",
                                    "levelConstraints": {
                                        "framerate": {
                                            "max": 960,
                                            "min": 0
                                        },
                                        "height": {
                                            "max": 2200,
                                            "min": 64
                                        },
                                        "width": {
                                            "max": 3900,
                                            "min": 64
                                        }
                                    },
                                    "maxLevel": "6.2",
                                    "profiles": [
                                        "baseline",
                                        "main",
                                        "high"
                                    ]
                                },
                                {
                                    "codec": "h265",
                                    "levelConstraints": {
                                        "framerate": {
                                            "max": 960,
                                            "min": 0
                                        },
                                        "height": {
                                            "max": 2200,
                                            "min": 144
                                        },
                                        "width": {
                                            "max": 3900,
                                            "min": 144
                                        }
                                    },
                                    "maxLevel": "6.2",
                                    "profiles": [
                                        "main",
                                        "main10"
                                    ]
                                }
                            ],
                            "hdrFormats": [
                                'dolbyvision8', 'dolbyvision5', 'dolbyvision',
                                'hdr10plus', 'hdr10', 'hlg'
                            ]
                        }
                    },
                    "contentProtection": {
                        "contentDecryptionModules": [
                            {
                                "drmKeySystem": 'playready',
                                "maxSecurityLevel": 'sl3000',

                            }
                        ]
                    },
                    "devicePlatform": {
                        "network": {
                            "capabilities": {
                                "protocols": {
                                    "http": {"byteRangeRequests": True}
                                }
                            },
                            "lastKnownStatus": {"networkTransportType": "wifi"}
                        },
                        "videoSink": {
                            "capabilities": {
                                "colorGamuts": ["standard"],
                                "hdrFormats": []
                            },
                            "lastKnownStatus": {
                                "height": 2200,
                                "width": 3900
                            }
                        }
                    },
                    "manifests": {"formats": {"dash": {}}}
                },
                "consumptionType": "streaming",
                "deviceInfo": {
                    "browser": {
                        "name": "Discovery Player Android androidTV",
                        "version": "1.8.1-canary.102"
                    },
                    "deviceId": "",
                    "deviceType": "androidtv",
                    "make": "NVIDIA",
                    "model": "SHIELD Android TV",
                    "os": {
                        "name": "ANDROID",
                        "version": "10"
                    },
                    "platform": "android",
                    "player": {
                        "mediaEngine": {
                            "name": "exoPlayer",
                            "version": "1.2.1"
                        },
                        "playerView": {
                            "height": 2160,
                            "width": 3840
                        },
                        "sdk": {
                            "name": "Discovery Player Android androidTV",
                            "version": "1.8.1-canary.102"
                        }
                    }
                },
                "editId": edit_id,
                "firstPlay": True,
                "gdpr": False,
                "playbackSessionId": str(uuid.uuid4()),
                #'applicationSessionId': str(uuid.uuid4()),
                "userPreferences": {"uiLanguage": "en"}
            }
        )

        playback_data = response.json()
        
        # TEST
        video_info = next(x for x in playback_data['videos'] if x['type'] == 'main')
        title.original_lang = Language.get(video_info['defaultAudioSelection']['language'])

        fallback_url = playback_data["fallback"]["manifest"]["url"]
        fallback_url = fallback_url.replace('fly', 'akm').replace('gcp', 'akm')

        try:
            self.pr_license_url = playback_data["drm"]["schemes"]["playready"]["licenseUrl"]
            drm_protection_enabled = True
        except (KeyError, IndexError):
            drm_protection_enabled = False

        try:
            self.wv_license_url = playback_data["drm"]["schemes"]["widevine"]["licenseUrl"]
            drm_protection_enabled = True
        except (KeyError, IndexError):
            drm_protection_enabled = False

        manifest_url = fallback_url.replace('_fallback', '')
        self.log.debug(f"Manifest URL: {manifest_url}")
        
        # ==============================================
        # FIXED PATCH - WITHOUT DEPENDING ON self.ctx
        # ==============================================
        import tempfile
        import subprocess
        import os
        import json
        
        # Create temporary file for MPD
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mpd', delete=False, encoding='utf-8') as tmp:
            mpd_temp_path = tmp.name
        
        # Variable for cookie_str (avoids "referenced before assignment")
        cookie_str = ""
        
        try:
            # ===================================================================
            # GET COOKIES - MULTIPLE SOURCES
            # ===================================================================
            essential_cookies = ['st', 'session', 'transientID']
            filtered_cookies = {}
            
            # METHOD 1: Look in fuckdl cookies folder
            cookies_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'cookies')
            cookies_dir = os.path.abspath(cookies_dir)
            cookies_file = os.path.join(cookies_dir, "hbomax.txt")
            
            self.log.debug(f"Looking for cookies in: {cookies_file}")
            
            if os.path.exists(cookies_file):
                self.log.debug(f"✅ Cookie file found")
                with open(cookies_file, 'r', encoding='utf-8', errors='ignore') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('#') or not line or '\t' not in line:
                            continue
                        
                        parts = line.split('\t')
                        if len(parts) >= 7:
                            cookie_name = parts[5]
                            cookie_value = parts[6]
                            
                            if cookie_name in essential_cookies:
                                filtered_cookies[cookie_name] = cookie_value
                                self.log.debug(f"  Cookie: {cookie_name} = {cookie_value[:20]}...")
            
            # METHOD 2: If no file, use session cookies
            if not filtered_cookies:
                self.log.debug("Using session cookies...")
                try:
                    cookie_dict = self.session.cookies.get_dict()
                    filtered_cookies = {k: v for k, v in cookie_dict.items() if k in essential_cookies}
                except Exception as e:
                    self.log.debug(f"Could not get session cookies: {e}")
            
            if not filtered_cookies:
                self.log.error("❌ No essential cookies found!")
                # Try hardcoded list of common cookies
                self.log.info("Trying with common cookies...")
                filtered_cookies = {}
                if hasattr(self.session, 'cookies'):
                    all_cookies = self.session.cookies.get_dict()
                    for cookie in ['st', 'session', 'transientID', 'sso', 'access_token']:
                        if cookie in all_cookies:
                            filtered_cookies[cookie] = all_cookies[cookie]
            
            self.log.debug(f"Cookies to use: {list(filtered_cookies.keys())}")
            
            if not filtered_cookies:
                raise ValueError("No authentication cookies found")
            
            # ===================================================================
            # BUILD COOKIE STRING
            # ===================================================================
            cookie_parts = []
            for key, value in filtered_cookies.items():
                if key == 'session' and value.startswith('{'):
                    try:
                        # Try to escape JSON
                        session_json = json.loads(value)
                        escaped_value = json.dumps(session_json).replace('"', '\\"')
                        cookie_parts.append(f'{key}={escaped_value}')
                    except:
                        cookie_parts.append(f'{key}={value}')
                else:
                    cookie_parts.append(f'{key}={value}')
            
            cookie_str = '; '.join(cookie_parts)
            self.log.debug(f"Cookie string prepared ({len(cookie_str)} chars)")
            
            # ===================================================================
            # MAIN METHOD: SIMPLE CURL
            # ===================================================================
            # Use the SAME command that worked in your manual test
            curl_cmd = f'curl -s -L -H "User-Agent: Mozilla/5.0" -H "Referer: https://play.hbomax.com/" -H "Cookie: {cookie_str}" -o "{mpd_temp_path}" "{manifest_url}"'
            
            self.log.debug(f"Executing curl: {curl_cmd[:100]}...")
            
            result = subprocess.run(curl_cmd, shell=True, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                with open(mpd_temp_path, 'r', encoding='utf-8') as f:
                    manifest_data = f.read()
                
                # Verify if it's valid MPD
                if '<MPD' in manifest_data or '<mpd' in manifest_data:
                    self.log.debug(f"✅ MPD downloaded successfully ({len(manifest_data)} bytes)")
                    
                    tracks: Tracks = Tracks.from_mpd(
                        url=manifest_url,
                        data=manifest_data,
                        source=self.ALIASES[0]
                    )
                    
                else:
                    # DEBUG: Save what was received
                    debug_content = manifest_data[:500]
                    self.log.error(f"❌ Not valid MPD. Content: {debug_content}")
                    
                    # Check if it's the 400 error
                    if "Request Header Or Cookie Too Large" in manifest_data:
                        self.log.error("⚠️  ERROR: Still sending too many cookies/headers")
                        self.log.debug(f"Cookie string used: {cookie_str[:100]}...")
                        raise ValueError("Headers too large")
                    else:
                        raise ValueError("Response is not MPD")
            else:
                self.log.error(f"❌ CURL failed: {result.stderr[:200]}")
                raise ValueError(f"CURL error: {result.returncode}")
            
        except Exception as e:
            self.log.error(f"❌ Error in main method: {e}")
            
            # ===================================================================
            # FALLBACK: TRY WITHOUT COOKIES OR WITH FEWER HEADERS
            # ===================================================================
            try:
                self.log.info("🔄 Trying simple fallback...")
                
                # Test 1: Only with Referer (no cookies)
                curl_simple = f'curl -s -L -H "Referer: https://play.hbomax.com/" -o "{mpd_temp_path}" "{manifest_url}"'
                result = subprocess.run(curl_simple, shell=True, capture_output=True, text=True, timeout=20)
                
                if result.returncode == 0:
                    with open(mpd_temp_path, 'r', encoding='utf-8') as f:
                        manifest_data = f.read()
                    
                    if '<MPD' in manifest_data or '<mpd' in manifest_data:
                        self.log.debug("✅ MPD obtained without cookies (surprise!)")
                        
                        tracks: Tracks = Tracks.from_mpd(
                            url=manifest_url,
                            data=manifest_data,
                            source=self.ALIASES[0]
                        )
                    else:
                        # Test 2: Only with st token (most important cookie)
                        if 'st' in filtered_cookies:
                            st_cookie = filtered_cookies['st']
                            curl_st_only = f'curl -s -L -H "Referer: https://play.hbomax.com/" -H "Cookie: st={st_cookie}" -o "{mpd_temp_path}" "{manifest_url}"'
                            
                            result = subprocess.run(curl_st_only, shell=True, capture_output=True, text=True, timeout=20)
                            
                            if result.returncode == 0:
                                with open(mpd_temp_path, 'r', encoding='utf-8') as f:
                                    manifest_data = f.read()
                                
                                if '<MPD' in manifest_data or '<mpd' in manifest_data:
                                    self.log.debug("✅ MPD obtained only with st token")
                                    
                                    tracks: Tracks = Tracks.from_mpd(
                                        url=manifest_url,
                                        data=manifest_data,
                                        source=self.ALIASES[0]
                                    )
                                else:
                                    raise ValueError("Fallback also failed")
                            else:
                                raise ValueError("CURL with only st failed")
                        else:
                            raise ValueError("No st token available")
                else:
                    raise ValueError("Simple CURL failed")
                    
            except Exception as e2:
                self.log.error(f"❌ All methods failed: {e2}")
                
                # ===================================================================
                # LAST ATTEMPT: HBO FALLBACK URL
                # ===================================================================
                try:
                    self.log.info("🔥 Last attempt: HBO fallback URL...")
                    
                    response = self.session.get(fallback_url, timeout=30)
                    response.raise_for_status()
                    manifest_data = response.text
                    
                    tracks: Tracks = Tracks.from_mpd(
                        url=fallback_url,
                        data=manifest_data,
                        source=self.ALIASES[0]
                    )
                    self.log.debug("✅ MPD obtained via fallback URL")
                    
                except Exception as e3:
                    self.log.error(f"💥 Total failure: {e3}")
                    raise
        finally:
            # Clean up temporary file
            try:
                if os.path.exists(mpd_temp_path):
                    os.unlink(mpd_temp_path)
            except:
                pass
        
        # ==============================================
        # TRACK PROCESSING (same as before)
        # ==============================================
        tracks.videos = self.dedupe(tracks.videos)
        tracks.audios = self.dedupe(tracks.audios)

        # remove partial subs
        tracks.subtitles.clear()

        subtitles = self.get_subtitles(manifest_url, manifest_data)
        
        subs = []
        for subtitle in subtitles:
            url = subtitle["url"][0] if isinstance(subtitle["url"], list) else subtitle["url"]
            subs.append(
                TextTrack(
                    id_=md5(url.encode()).hexdigest(),
                    source=self.ALIASES[0],
                    url=subtitle["url"],
                    codec=subtitle['format'],
                    language=subtitle["language"],
                    forced=subtitle['name'] == 'Forced',
                    sdh=subtitle['name'] == 'SDH'
                )
            )

        tracks.add(subs)

        if self.vcodec:
            tracks.videos = [x for x in tracks.videos if (x.codec or "")[:4] in self.VIDEO_CODEC_MAP[self.vcodec]]

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        for track in tracks:
            track.needs_proxy = False
            if isinstance(track, VideoTrack):
                codec = track.extra[0].get("codecs")
                supplementalcodec = track.extra[0].get("{urn:scte:dash:scte214-extensions}supplementalCodecs") or ""
                #track.hdr10 = codec[0:4] in ("hvc1", "hev1") and codec[5] == "2"
                track.hdr10 = track.dvhdr
                track.dv = codec[0:4] in ("dvh1", "dvhe") or supplementalcodec[0:4] in ("dvh1", "dvhe")
                    
            if isinstance(track, TextTrack) and track.codec == "":
                track.codec = "webvtt"
                
            if isinstance(track, AudioTrack):
                role = track.extra[1].find("Role")
                if role is not None and role.get("value") in ["description", "alternative", "alternate"]:
                    track.descriptive = True

        title.service_data['info'] = video_info

        return tracks

    def get_chapters(self, title: Title):
        chapters = []
        video_info = title.service_data['info']
        if 'annotations' in video_info:
            chapters.append(MenuTrack(number=1, title='Chapter 1', timecode='00:00:00.0000'))
            chapters.append(MenuTrack(number=2, title='Credits', timecode=self.convert_timecode(video_info['annotations'][0]['start'])))
            chapters.append(MenuTrack(number=3, title='Chapter 2', timecode=self.convert_timecode(video_info['annotations'][0]['end'])))

        return chapters

    def certificate(self, challenge, **_):
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            return None
        else:
            return self.license(challenge)

    def license(self, challenge, **_):
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            decoded_challenge = str(challenge.decode())
            return self.session.post(
                url=self.pr_license_url,
                data=decoded_challenge  # expects XML
            ).content
        else:
            return self.session.post(
                url=self.wv_license_url,
                data=challenge  # expects bytes
            ).content

    def configure(self):
        token = self.session.cookies.get_dict()["st"]
        device_id = json.loads(self.session.cookies.get_dict()["session"])
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            self.session.headers.update({
                'User-Agent': 'BEAM-Android/5.0.0 (motorola/moto g(6) play)',
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-disco-client': 'ANDROID:9:beam:5.0.0',
                'x-disco-params': 'realm=bolt,bid=beam,features=ar,rr',
                'x-device-info': 'BEAM-Android/5.0.0 (motorola/moto g(6) play; ANDROID/9; 9cac27069847250f/b6746ddc-7bc7-471f-a16c-f6aaf0c34d26)',
                'traceparent': '00-053c91686df1e7ee0b0b0f7fda45ee6a-f5a98d6877ba2515-01',
                'tracestate': f'wbd=session:{device_id}',
                'Origin': 'https://play.hbomax.com',
                'Referer': 'https://play.hbomax.com/',
            })
        else:
            self.session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/113.0',
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'x-disco-client': 'WEB:NT 10.0:beam:0.0.0',
                'x-disco-params': 'realm=bolt,bid=beam,features=ar',
                'x-device-info': 'beam/0.0.0 (desktop/desktop; Windows/NT 10.0; b3950c49-ed17-49d0-beb2-11b1d61e5672/da0cdd94-5a39-42ef-aa68-54cbc1b852c3)',
                'traceparent': '00-053c91686df1e7ee0b0b0f7fda45ee6a-f5a98d6877ba2515-01',
                'tracestate': f'wbd=session:{device_id}',
                'Origin': 'https://play.hbomax.com',
                'Referer': 'https://play.hbomax.com/',
            })

        auth_token = self.get_device_token()
        self.session.headers.update({
            "x-wbd-session-state": auth_token
        })

    def get_device_token(self):
        response = self.session.post(
            'https://default.any-any.prd.api.hbomax.com/session-context/headwaiter/v1/bootstrap', # any-any can be removed
        )
        response.raise_for_status()

        return response.headers.get('x-wbd-session-state')

    @staticmethod
    def convert_timecode(time):
        secs, ms = divmod(time, 1)
        mins, secs = divmod(secs, 60)
        hours, mins = divmod(mins, 60)
        ms = ms * 10000
        chapter_time = '%02d:%02d:%02d.%04d' % (hours, mins, secs, ms)

        return chapter_time

    def get_subtitles(self, manifest_url, manifest_data):
        xml = xmltodict.parse(manifest_data)
        periods = xml["MPD"]["Period"]
        if isinstance(periods, dict):
            periods = [periods]
        period = periods[-1] # Grab the last period

        adaptation_sets = period.get("AdaptationSet", [])
        if isinstance(adaptation_sets, dict):
            adaptation_sets = [adaptation_sets]

        subtitles = []
        success_count = 0
        fallback = False
        for adaptation_set in adaptation_sets:
            if adaptation_set["@contentType"] != "text":
                continue

            rep = adaptation_set["Representation"]
            if isinstance(rep, list):
                rep = rep[0]

            sub_types = {
                "sdh": ("_sdh.vtt", "SDH"),
                "caption": ("_cc.vtt", "SDH"),
                "subtitle": ("_sub.vtt", "Full"),
                "forced-subtitle": ("_forced.vtt", "Forced"),
            }

            is_sdh = "sdh" in adaptation_set["Label"].lower()
            language = adaptation_set["@lang"]
            sub_type = "sdh" if is_sdh else adaptation_set["Role"]["@value"]
            sub_path = rep["SegmentTemplate"]["@media"]
            segments = int(rep["SegmentTemplate"]["@startNumber"]) # The startsnumber will be the highest for the last period ie != 1
            base_url = manifest_url.rsplit('/', 1)[0]
            suffix, name = sub_types[sub_type]

            path = "/".join(sub_path.split("/", 2)[:2])
            url = f"{base_url}/{path}/{language}{suffix}"

            if success_count < 3 and not fallback:
                try:
                    res = self.session.head(url=url)
                    success_count += 1
                except requests.exceptions.RequestException:
                    fallback = True
                    self.log.warning("Falling back to segmented subs...")
            
            if fallback:
                url = [
                    f"{base_url}/{sub_path}".replace("$Number$", str(x))
                    for x in range(1, segments + 1)
                ]
            
            subtitles.append({
                "url": url,
                "format": "vtt",
                "language": language,
                "name": name,
            })

        return self.remove_dupe(subtitles)

    @staticmethod
    def force_instance(data, variable):
        if isinstance(data[variable], list):
            X = data[variable]
        else:
            X = [data[variable]]
        return X

    @staticmethod
    def remove_dupe(items):
        seen_urls = set()
        unique_items = []

        for item in items:
            url = item['url']
            url = tuple(url) if isinstance(url, list) else url

            if url not in seen_urls:
                unique_items.append(item)
                seen_urls.add(url)

        return unique_items
        
    @staticmethod
    def dedupe(items: list) -> list:
        if isinstance(items[0].url, list):
            return items

        filtered_items = list({item.url: item for item in items}.values())

        return filtered_items
