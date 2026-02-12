import base64
import json
import re
from datetime import datetime
from urllib.parse import unquote
from typing import List, Optional, Dict, Any

import click
import m3u8
import requests

from fuckdl.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from fuckdl.services.BaseService import BaseService
from fuckdl.utils.collections import as_list
from fuckdl.utils import try_get
from fuckdl.vendor.pymp4.parser import Box
from fuckdl.utils.widevine.device import LocalDevice


class AppleTVPlus(BaseService):
    """
    Service code for Apple's TV Plus streaming service (https://tv.apple.com).

    Authorization: Cookies
    Security: UHD@L1 FHD@L1 HD@L3
    """

    ALIASES = ["ATVP", "appletvplus", "appletv+"]
    TITLE_RE = r"^(?:https?://tv\.apple\.com(?:/[a-z]{2})?/(?:movie|show|episode)/[a-z0-9-]+/)?(?P<id>umc\.cmc\.[a-z0-9]+)"

    VIDEO_CODEC_MAP = {
        "H264": ["avc"],
        "H265": ["hvc", "hev", "dvh"]
    }
    
    AUDIO_CODEC_MAP = {
        "AAC": ["HE", "stereo"],
        "AC3": ["ac3"],
        "EC3": ["ec3", "atmos"]
    }

    @staticmethod
    @click.command(name="AppleTVPlus", short_help="https://tv.apple.com")
    @click.argument("title", type=str, required=False)
    @click.pass_context
    def cli(ctx, **kwargs):
        return AppleTVPlus(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)
        self.cdm = ctx.obj.cdm
        self.vcodec = ctx.parent.params["vcodec"]
        self.acodec = ctx.parent.params["acodec"]
        self.alang = ctx.parent.params["alang"]
        self.subs_only = ctx.parent.params["subs_only"]
        
        self.extra_server_parameters = None
        self.storefront = None
        self.environment_config = None
        
        self.configure()

    def get_titles(self) -> List[Title]:
        """Get title(s) based on the provided ID."""
        title_info = self._get_title_info()
        
        if not title_info:
            raise self.log.exit(f" - Title ID {self.title!r} could not be found.")
        
        if title_info["type"] == "Movie":
            return self._process_movie_title(title_info)
        else:
            return self._process_tv_series(title_info)

    def get_tracks(self, title: Title) -> Tracks:
        """Get available tracks for the title."""
        stream_data = self._get_stream_data(title.service_data["id"])
        
        if not stream_data["isEntitledToPlay"]:
            raise self.log.exit(" - User is not entitled to play this title")
        
        self.extra_server_parameters = stream_data["assets"]["fpsKeyServerQueryParameters"]
        hls_url = stream_data["assets"]["hlsUrl"]
        
        self.log.info(f" - Fetching HLS manifest from: {hls_url}")
        master_playlist = self._fetch_hls_manifest(hls_url)
        tracks = self._parse_tracks_from_hls(master_playlist, hls_url)
        
        self.watermarktoken = None
        for track in tracks:
            if isinstance(track, VideoTrack) and 'watermarkingToken=' in track.url:
                match = re.search(r'watermarkingToken=([^&]+)', track.url)
                if match:
                    self.watermarktoken = unquote(match.group(1))
                    self.log.info(f" - Found watermarking token: {self.watermarktoken}")
                    break
        
        tracks = self._filter_and_enhance_tracks(tracks)
        return tracks

    def get_chapters(self, title: Title) -> List:
        """Get chapters for the title (currently not supported)."""
        return []

    def certificate(self, **_) -> Optional[bytes]:
        """Get Widevine certificate."""
        return None  # Uses common privacy cert

    def license(self, challenge: bytes, track, title, **_) -> bytes:
        """Get license for the track."""
        return self._request_license(challenge, track)

    # Helper methods

    def configure(self):
        """Configure session with necessary headers and tokens."""
        self._set_storefront()
        self._set_environment_config()
        self._update_session_headers()

    def _get_title_info(self) -> Optional[Dict]:
        """Get title information from API."""
        for media_type in ["shows", "movies"]:
            try:
                params = self._get_base_params()
                params["sf"] = self.storefront
                
                response = self.session.get(
                    url=self.config["endpoints"]["title"].format(
                        type=media_type, 
                        id=self.title
                    ),
                    params=params
                )
                response.raise_for_status()
                
                data = response.json()
                return data.get("data", {}).get("content")
                
            except requests.HTTPError as e:
                if e.response.status_code != 404:
                    raise
            except json.JSONDecodeError:
                self.log.error(f"Failed to parse JSON response for {media_type}")
        
        return None

    def _process_movie_title(self, title_info: Dict) -> List[Title]:
        """Process movie title information."""
        release_date = title_info.get("releaseDate")
        year = None
        if release_date:
            try:
                year = datetime.utcfromtimestamp(release_date / 1000).year
            except (TypeError, ValueError):
                pass
        
        return [Title(
            id_=self.title,
            type_=Title.Types.MOVIE,
            name=title_info["title"],
            year=year,
            original_lang=title_info.get("originalSpokenLanguages", [{}])[0].get("locale", "en"),
            source=self.ALIASES[0],
            service_data=title_info
        )]

    def _process_tv_series(self, title_info: Dict) -> List[Title]:
        """Process TV series episodes."""
        params = self._get_base_params()
        params["sf"] = self.storefront
        
        response = self.session.get(
            url=self.config["endpoints"]["tv_episodes"].format(id=self.title),
            params=params
        )
        response.raise_for_status()
        
        data = response.json()
        episodes = data.get("data", {}).get("episodes", [])
        
        titles = []
        for episode in episodes:
            titles.append(Title(
                id_=self.title,
                type_=Title.Types.TV,
                name=episode.get("showTitle", ""),
                season=episode.get("seasonNumber", 1),
                episode=episode.get("episodeNumber", 1),
                episode_name=episode.get("title"),
                original_lang=title_info.get("originalSpokenLanguages", [{}])[0].get("locale", "en"),
                source=self.ALIASES[0],
                service_data=episode
            ))
        
        return titles

    def _get_stream_data(self, content_id: str) -> Dict:
        """Get stream data for content ID."""
        params = self._get_base_params()
        params["sf"] = self.storefront
        
        response = self.session.get(
            url=self.config["endpoints"]["manifest"].format(id=content_id),
            params=params
        )
        response.raise_for_status()
        
        data = response.json()
        return data.get("data", {}).get("content", {}).get("playables", [{}])[0]

    def _fetch_hls_manifest(self, hls_url: str) -> m3u8.M3U8:
        """Fetch and parse HLS manifest."""
        headers = {
            'User-Agent': self.config.get("user_agent", 'AppleTV6,2/11.1')
        }
        
        response = requests.get(url=hls_url, headers=headers)
        response.raise_for_status()
        
        return m3u8.loads(response.text, hls_url)

    def _parse_tracks_from_hls(self, master_playlist: m3u8.M3U8, base_url: str) -> Tracks:
        """Parse tracks from HLS master playlist."""
        tracks = Tracks.from_m3u8(
            master=master_playlist,
            source=self.ALIASES[0]
        )
        
        # Store original manifest data
        for track in tracks:
            if hasattr(track, 'extra'):
                track.extra = {"manifest": track.extra}
            else:
                track.extra = {"manifest": None}
        
        return tracks

    def _filter_and_enhance_tracks(self, tracks: Tracks) -> Tracks:
        """Filter and enhance track information."""
        # Filter video tracks by codec
        if self.vcodec and self.vcodec in self.VIDEO_CODEC_MAP:
            tracks.videos = [
                x for x in tracks.videos 
                if any(codec in (x.codec or "").lower() for codec in self.VIDEO_CODEC_MAP[self.vcodec])
            ]
        
        # Filter audio tracks by codec
        if self.acodec and self.acodec in self.AUDIO_CODEC_MAP:
            tracks.audios = [
                x for x in tracks.audios 
                if any(codec in (x.codec or "").lower() for codec in self.AUDIO_CODEC_MAP[self.acodec])
            ]
        
        # Enhance track information
        for track in tracks:
            self._enhance_track_info(track)
        
        # Filter subtitle tracks
        tracks.subtitles = [
            x for x in tracks.subtitles
            if (x.language in self.alang or 
                (x.is_original_lang and "orig" in self.alang) or 
                "all" in self.alang)
            or self.subs_only
            or not x.sdh
        ]
        
        # Filter by CDN (keep only vod-ak CDN for consistency)
        filtered_tracks = Tracks([
            x for x in tracks if "vod-ak" in x.url
        ])
        
        return filtered_tracks

    def _enhance_track_info(self, track):
        """Enhance track with additional information."""
        if isinstance(track, VideoTrack):
            track.encrypted = True
            track.needs_ccextractor_first = True
            
            # Try to determine quality from URL
            if track.extra.get("manifest") and track.extra["manifest"].uri:
                uri = track.extra["manifest"].uri
                for quality_str, quality_val in self.config["quality_map"].items():
                    if quality_str.lower() in uri.lower():
                        track.extra["quality"] = quality_val
                        break
        
        elif isinstance(track, AudioTrack):
            track.encrypted = True
            
            # Extract bitrate from URL
            bitrate_match = re.search(r"&g=(\d+?)&", track.url) or re.search(r"_gr(\d+)_", track.url)
            if bitrate_match:
                bitrate_str = bitrate_match.group(1)
                if len(bitrate_str) >= 3:
                    track.bitrate = int(bitrate_str[-3:]) * 1000
            
            # Clean up codec string
            if track.codec:
                track.codec = track.codec.replace("_vod", "")
        
        elif isinstance(track, TextTrack):
            track.codec = "vtt"

    def _request_license(self, challenge: bytes, track) -> bytes:
        """Request license from Apple's license server."""
        license_request = self._build_license_request(challenge, track)
        
        try:
            response = self.session.post(
                url=self.config["endpoints"]["license"],
                json=license_request
            )
            response.raise_for_status()
            
            data = response.json()
            
            # Validate response structure
            if "streaming-response" not in data:
                raise ValueError("Invalid license response: missing streaming-response")
            
            streaming_keys = data["streaming-response"].get("streaming-keys", [])
            if not streaming_keys:
                raise ValueError("No streaming keys in license response")
            
            license_data = streaming_keys[0].get("license")
            if not license_data:
                raise ValueError("No license data in streaming key")
            
            return base64.b64decode(license_data)
            
        except requests.HTTPError as e:
            self.log.error(f"License request failed: {e}")
            if e.response.text:
                try:
                    error_data = e.response.json()
                    self.log.error(f"Error details: {error_data}")
                except:
                    self.log.error(f"Raw error: {e.response.text}")
            raise

    def _build_license_request(self, challenge: bytes, track) -> Dict:
        """Build license request based on CDM type."""
        if self.cdm.device.type == LocalDevice.Types.PLAYREADY:
            key_system = "com.microsoft.playready"
            
            uri_parts = ["data:text/plain"]
            
            if self.watermarktoken:
                uri_parts.append(f"watermarkingToken={self.watermarktoken}")
            
            uri_parts.append("charset=UTF-16")
            uri_parts.append(f"base64,{track.pr_pssh}")
            uri = ";".join(uri_parts)
            
            challenge_b64 = base64.b64encode(challenge).decode('utf-8')
        else:
            key_system = "com.widevine.alpha"
            pssh_box = Box.build(track.pssh) if hasattr(track, 'pssh') else b''
            uri = f"data:text/plain;base64,{base64.b64encode(pssh_box).decode()}"
            challenge_b64 = base64.b64encode(challenge).decode()
        
        streaming_keys = {
            "challenge": challenge_b64,
            "key-system": key_system,
            "uri": uri,
            "id": 0 if key_system == "com.microsoft.playready" else 1,
            "lease-action": 'start',
            "adamId": self.extra_server_parameters.get('adamId', ''),
            "isExternal": True,
            "svcId": self.extra_server_parameters.get('svcId', ''),
        }
        
        if self.extra_server_parameters:
            streaming_keys["extra-server-parameters"] = self.extra_server_parameters
        
        return {
            'streaming-request': {
                'version': 1,
                'streaming-keys': [streaming_keys],
            }
        }

    def _set_storefront(self):
        """Set storefront based on cookies."""
        try:
            # Obtener todas las cookies itua
            itu_cookies = []
            for cookie in self.session.cookies:
                if cookie.name == "itua":
                    itu_cookies.append({
                        'value': cookie.value,
                        'domain': cookie.domain,
                        'path': cookie.path,
                        'expires': cookie.expires
                    })
            
            if not itu_cookies:
                raise ValueError("Missing 'itua' cookie")
            
            # Log de las cookies encontradas
            self.log.debug(f"Found {len(itu_cookies)} 'itua' cookies:")
            for i, cookie in enumerate(itu_cookies, 1):
                self.log.debug(f"  {i}. Value: {cookie['value']}, Domain: {cookie['domain']}")
            
            # Estrategia para elegir la cookie correcta:
            # 1. Priorizar cookies de tv.apple.com
            # 2. Priorizar cookies más recientes (mayor expiry)
            tv_cookies = [c for c in itu_cookies if 'tv.apple.com' in c['domain']]
            
            if tv_cookies:
                # Usar la cookie de tv.apple.com con expiry más lejano (más reciente)
                selected_cookie = max(tv_cookies, key=lambda x: x['expires'] or 0)
            else:
                # Usar la cookie con expiry más lejano
                selected_cookie = max(itu_cookies, key=lambda x: x['expires'] or 0)
            
            itu_cookie_value = selected_cookie['value']
            self.log.info(f"Selected 'itua' cookie from {selected_cookie['domain']}")
            
            # Obtener mapeo de storefront
            try:
                response = requests.get(
                    self.config["storefront_mapping_url"],
                    timeout=10
                )
                response.raise_for_status()
                mappings = response.json()
                
                # Buscar storefront
                for mapping in mappings:
                    if mapping.get('code') == itu_cookie_value:
                        self.storefront = mapping.get('storefrontId')
                        break
                
                if not self.storefront:
                    # Intentar con las primeras 2 letras (código de país)
                    country_code = itu_cookie_value[:2].upper()
                    for mapping in mappings:
                        if mapping.get('code', '').upper() == country_code:
                            self.storefront = mapping.get('storefrontId')
                            break
                    
                    if not self.storefront:
                        raise ValueError(f"Storefront not found for country code: {itu_cookie_value}")
                
                self.log.info(f"Using storefront: {self.storefront}")
                
            except requests.RequestException as e:
                self.log.error(f"Failed to fetch storefront mapping: {e}")
                # Fallback a storefront por defecto basado en el dominio
                self._set_fallback_storefront(itu_cookie_value)
                
        except Exception as e:
            self.log.error(f"Error setting storefront: {e}")
            raise

    def _set_fallback_storefront(self, country_code):
        """Set fallback storefront based on country code."""
        # Mapeo común de códigos de país a storefronts
        common_storefronts = {
            'US': '143441',
            'GB': '143444',
            'DE': '143443',
            'FR': '143442',
            'CA': '143455',
            'AU': '143460',
            'JP': '143462',
            'BR': '143503',
            'MX': '143468',
            'ES': '143454',
        }
        
        # Intentar con las primeras 2 letras
        short_code = country_code[:2].upper()
        
        if short_code in common_storefronts:
            self.storefront = common_storefronts[short_code]
            self.log.warning(f"Using fallback storefront {self.storefront} for {short_code}")
        else:
            # Fallback a US
            self.storefront = '143441'
            self.log.warning(f"Using default US storefront: {self.storefront}")

    def _set_environment_config(self):
        """Get environment configuration from Apple TV+ page."""
        try:
            response = self.session.get(self.config["endpoints"]["environment"])
            response.raise_for_status()
            
            # Parse serialized server data
            script_pattern = r'<script[^>]*id=["\']serialized-server-data["\'][^>]*>(.*?)</script>'
            match = re.search(script_pattern, response.text, re.DOTALL)
            
            if match:
                script_content = match.group(1).strip()
                data = json.loads(script_content)
                
                if (data and len(data) > 0 and 
                    'data' in data[0] and 
                    'configureParams' in data[0]['data']):
                    self.environment_config = data[0]['data']['configureParams']
            
            if not self.environment_config:
                raise ValueError("Failed to extract environment configuration")
                
        except Exception as e:
            self.log.error(f"Failed to get environment config: {e}")
            raise

    def _update_session_headers(self):
        """Update session headers with authentication."""
        if not self.environment_config or 'developerToken' not in self.environment_config:
            raise ValueError("Missing developer token in environment config")
        
        # Manejar múltiples cookies media-user-token
        media_cookies = []
        for cookie in self.session.cookies:
            if cookie.name == "media-user-token":
                media_cookies.append({
                    'value': cookie.value,
                    'domain': cookie.domain,
                    'path': cookie.path,
                    'expires': cookie.expires
                })
        
        if not media_cookies:
            raise ValueError("Missing 'media-user-token' cookie")
        
        # Elegir la cookie correcta (similar estrategia que con itua)
        tv_cookies = [c for c in media_cookies if 'tv.apple.com' in c['domain']]
        
        if tv_cookies:
            # Usar la cookie de tv.apple.com con expiry más lejano
            selected_cookie = max(tv_cookies, key=lambda x: x['expires'] or 0)
        else:
            # Usar la cookie con expiry más lejano
            selected_cookie = max(media_cookies, key=lambda x: x['expires'] or 0)
        
        media_token = selected_cookie['value']
        self.log.info(f"Selected 'media-user-token' cookie from {selected_cookie['domain']}")
        
        self.session.headers.update({
            "User-Agent": self.config.get("user_agent", "AppleTV6,2/11.1"),
            "Authorization": f"Bearer {self.environment_config['developerToken']}",
            "media-user-token": media_token,
            "x-apple-music-user-token": media_token,
            **self.config.get("headers", {})
        })

    def _get_base_params(self) -> Dict:
        """Get base parameters for API requests."""
        return self.config.get("params", {}).copy()