# fuckdl/services/youtube.py

# Created on: 2024-01-01
# Authors: Amorä¸¶Aprca
# Final Version: 4.0 

from __future__ import annotations

import click
import hashlib
import re
import json
import base64
from typing import Any
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from fuckdl.utils import try_get

from fuckdl.objects import Title, Tracks, TextTrack
from fuckdl.services.BaseService import BaseService

class YouTube(BaseService):
    """
    Service code for YouTube VOD (https://youtube.com)
    
    \b
    Authorization: Cookies
    Robustness:
      Widevine:
        L3: Up to 1080p

    \b
    Example (Single Video/watch):
    fuckdl dl youtube dQw4w9WgXcQ
    
    \b
    Example (Show/Series):
    fuckdl dl youtube https://www.youtube.com/show/SCguuq8upmH1TmPhSI3nqYqg
    
    \b
    Example (Show/Series with specific season):
    fuckdl dl youtube "https://www.youtube.com/show/SCfgsEfVI3WMNeynOVuBwghw?season=6"
    
    \b
    Example (Show/Series with specific season):
    Can only use -W S01E0X-S01E0X
    fuckdl dl -w S01E01-S01E03 youtube "https://www.youtube.com/show/SCfgsEfVI3WMNeynOVuBwghw?season=6"
    """

    ALIASES = ["YouTube", "youtube", "yt", "ytbe"] 
    TITLE_RE = r"^(?:https?://(?:www\.)?youtube\.com/(?:watch\?v=|show/))?(?P<id>[\w\-]+)"

    # Constants
    YOUTUBE_PLAYER_URL: str = 'https://www.youtube.com/youtubei/v1/player'
    LICENSE_SERVER_URL: str = 'https://www.youtube.com/youtubei/v1/player/get_drm_license'
    API_KEY: str = 'AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8'

    @staticmethod
    @click.command(name="YouTube", short_help="https://youtube.com", help=__doc__)
    @click.argument("title", type=str, required=True)
    @click.pass_context
    def cli(ctx, **kwargs):
        return YouTube(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.full_input_title = title
        self.parsed_title = self.parse_title(ctx, title)
        self.title_id = self.parsed_title.get("id") or title
        self.is_show = "/show/" in self.full_input_title or self.title_id.startswith("SC")
        self.context_data: dict[str, Any] = {}
        self.page_data: dict[str, Any] = {}

    def _initialize_context_and_page_data(self):
        if self.context_data:
            return

        self.log.info("Initializing YouTube API context...")
        
        if "youtube.com" in self.full_input_title:
            scrape_url = self.full_input_title
        else:
            scrape_url = f'https://www.youtube.com/show/{self.title_id}' if self.is_show else f'https://www.youtube.com/watch?v={self.title_id}'
        
        self.log.info(f"Scraping YouTube page for initial data: {scrape_url}")
        response = self.session.get(scrape_url)
        soup = BeautifulSoup(response.text, 'html.parser')

        ytcfg_set_content = {}
        yt_initial_data = {}
        yt_player_response = {} # [NEW] Capture player response from web

        for script in soup.find_all('script'):
            if not script.string: continue
            
            # Scrape ytInitialData
            if 'var ytInitialData = ' in script.string:
                match = re.search(r'var ytInitialData = ({.*?});', script.string, re.DOTALL)
                if match:
                    try: yt_initial_data = json.loads(match.group(1));
                    except json.JSONDecodeError: self.log.debug("Found ytInitialData but failed to parse JSON.")

            if 'var ytInitialPlayerResponse = ' in script.string:
                match = re.search(r'var ytInitialPlayerResponse = ({.*?});', script.string, re.DOTALL)
                if match:
                    try: yt_player_response = json.loads(match.group(1))
                    except json.JSONDecodeError: self.log.debug("Found ytInitialPlayerResponse but failed to parse JSON.")

            # Scrape ytcfg
            if 'ytcfg.set' in script.string:
                match = re.search(r'ytcfg\.set\s*\(\s*({.*?})\s*\)\s*;', script.string, re.DOTALL)
                if match:
                    try: ytcfg_set_content = json.loads(match.group(1))
                    except json.JSONDecodeError: self.log.debug("Found ytcfg.set but failed to parse JSON.")

        if not ytcfg_set_content: self.log.exit(" - Failed to extract ytcfg. Cannot proceed.")
        if self.is_show and not yt_initial_data: self.log.exit(" - Failed to extract ytInitialData for show page. Cannot proceed.")
        
        self.page_data['ytcfg'] = ytcfg_set_content
        self.page_data['ytInitialData'] = yt_initial_data
        self.page_data['ytInitialPlayerResponse'] = yt_player_response # [NEW] Store it

        client_context_data = try_get(ytcfg_set_content, lambda x: x['INNERTUBE_CONTEXT']['client']) or {}
        
        forced_user_agent = "Mozilla/5.0 (Linux; Tizen 2.3) AppleWebKit/538.1 (KHTML, like Gecko)Version/2.3 TV Safari/538.1"
        
        client_version = client_context_data.get('clientVersion', "2.20240502.01.00")
        id_token = ytcfg_set_content.get("ID_TOKEN")
        visitor_data = client_context_data.get("visitorData")
        session_index = ytcfg_set_content.get("SESSION_INDEX")

        cookies_d = {c.name: c.value for c in self.cookies}
        sapisid = cookies_d.get('__Secure-3PAPISID', cookies_d.get('SAPISID', ''))
        if not sapisid: self.log.warning("Could not find SAPISID cookie. Auth may fail.")
        
        epoch = str(int(datetime.now().timestamp()))
        origin = "https://www.youtube.com"
        sapisid_data = f"{epoch} {sapisid} {origin}"
        sha1 = hashlib.sha1(sapisid_data.encode('utf-8'))
        sapisidhash = f"SAPISIDHASH {epoch}_{sha1.hexdigest()}"

        api_headers = {
            'authorization': sapisidhash,
            'origin': origin,
            'user-agent': forced_user_agent,
            'x-youtube-client-version': client_version,
            'x-youtube-client-name': '2'
        }
        if id_token: api_headers['X-Youtube-Identity-Token'] = id_token
        if session_index: api_headers['x-goog-authuser'] = str(session_index)
        if visitor_data: api_headers['x-goog-visitor-id'] = visitor_data
        
        self.session.headers.update(api_headers)

        self.context_data['api_headers'] = api_headers
        self.context_data['client_info'] = {
            "clientName": "TVHTML5",
            "clientVersion": "7.20230803.00.00",
            "osName": "Tizen",
            "osVersion": "2.3",
            "platform": "TV",
            "remoteHost": "192.168.1.100",
            "visitorData": visitor_data,
            "userAgent": forced_user_agent,
        }
        if session_index: self.context_data['session_id'] = session_index

    def _create_context_payload(self, video_id: str) -> dict:
        """Creates the JSON payload for the player API request, using a forced TV context."""
        context_payload = {
            'context': {
                'client': self.context_data['client_info'],
                'playbackContext': {
                    'contentPlaybackContext': {
                        "currentUrl": f"/watch?v={video_id}",
                        "vis": 0,
                        "splay": False,
                        "autoCaptionsDefaultOn": False,
                        "autonavState": "STATE_OFF",
                        "html5Preference": "HTML5_PREF_SECURE_EXTRA_CODECS",
                        'lactMilliseconds': "15000"
                    }
                },
                'thirdParty': {'embedUrl': 'https://www.youtube.com/'}
            },
            'videoId': video_id,
            'racyCheckOk': True,
            'contentCheckOk': True
        }
        return context_payload

    def _get_show_titles(self) -> list[Title]:
        self.log.info("Processing as a YouTube Show...")
        self._initialize_context_and_page_data()
        
        initial_data = self.page_data['ytInitialData']
        
        show_name = try_get(initial_data, lambda x: x['sidebar']['playlistSidebarRenderer']['items'][0]['playlistSidebarPrimaryInfoRenderer']['title']['simpleText'])
        if not show_name:
            show_name = try_get(initial_data, lambda x: x['metadata']['showMetadataRenderer']['title']['simpleText'])
        if not show_name:
            show_name = try_get(initial_data, lambda x: x['header']['pageHeaderRenderer']['title']['simpleText'])
        
        if not show_name: self.log.exit(" - Could not determine show name from any known paths.")

        tabs = try_get(initial_data, lambda x: x['contents']['twoColumnBrowseResultsRenderer']['tabs'])
        if not tabs: self.log.exit(" - Could not find tabs in page data (ytInitialData). Structure may have changed.")
        
        titles = []
        
        content_renderers = try_get(tabs[0], lambda x: x['tabRenderer']['content']['sectionListRenderer']['contents'])
        if not content_renderers: self.log.exit(" - Could not find content renderers. Structure may have changed.")
        
        season_number = 1 
        
        for renderer in content_renderers:
            item_section = renderer.get('itemSectionRenderer', {})
            
            metadata_renderer = try_get(item_section, lambda x: x['contents'][0]['playlistShowMetadataRenderer'])
            if metadata_renderer:
                season_title = try_get(metadata_renderer, lambda x: x['collection']['sortFilterSubMenuRenderer']['subMenuItems'][0]['title'])
                season_match = re.search(r'(\d+)', season_title or "")
                if season_match:
                    season_number = int(season_match.group(1))

            video_list_renderer = try_get(item_section, lambda x: x['contents'][0]['playlistVideoListRenderer'])
            if video_list_renderer:
                episodes = video_list_renderer.get('contents', [])
                for episode_data in episodes:
                    video_renderer = episode_data.get('playlistVideoRenderer')
                    if not video_renderer: continue
                    
                    video_id = video_renderer.get('videoId')
                    episode_title = try_get(video_renderer, lambda x: x['title']['runs'][0]['text'])
                    episode_number_str = try_get(video_renderer, lambda x: x['index']['simpleText'])
                    
                    if not video_id or not episode_number_str: continue

                    try: episode_number = int(episode_number_str)
                    except (ValueError, TypeError):
                        self.log.warning(f"Could not parse episode number for video {video_id}. Skipping.")
                        continue

                    titles.append(Title(
                        id_=video_id, type_=Title.Types.TV, name=show_name,
                        season=season_number, episode=episode_number, episode_name=episode_title,
                        source=self.ALIASES[0]
                    ))
        
        if not titles: self.log.exit(" - No episodes found for this show. The URL might be for a specific season not on this page.")
        return titles

    def _get_single_video_title(self) -> list[Title]:
        self.log.info("Processing as a single YouTube video...")
        self._initialize_context_and_page_data()
        
        context_payload = self._create_context_payload(self.title_id)

        self.log.info("Fetching video info from YouTube API (with TV context)...")
        response = self.session.post(
            f"{self.YOUTUBE_PLAYER_URL}?key={self.API_KEY}", 
            json=context_payload
        )
        response.raise_for_status()
        video_info = response.json()

        if video_info.get('playabilityStatus', {}).get('status') != "OK":
            reason = video_info.get('playabilityStatus', {}).get('reason', 'Unknown reason')
            self.log.exit(f" - This video is not playable: {reason}")
        
        video_title = try_get(video_info, lambda x: x['videoDetails']['title'])
        
        if not video_title:
            # Fallback 1: Microformat from API
            video_title = try_get(video_info, lambda x: x['microformat']['playerMicroformatRenderer']['title']['simpleText'])
        
        if not video_title:
            # Fallback 2: Web Scraped Data (ytInitialPlayerResponse) - Matches your provided JSON
            web_response = self.page_data.get('ytInitialPlayerResponse', {})
            video_title = try_get(web_response, lambda x: x['videoDetails']['title'])
            if video_title:
                self.log.info(f"Found title in Web metadata: {video_title}")

        if not video_title:
            self.log.warning(" - Could not find video title in API or Web Metadata. Using video ID as a fallback.")
            video_title = self.title_id
        
        service_data = self.context_data.copy()
        service_data['video_info'] = video_info

        return [Title(
            id_=self.title_id, type_=Title.Types.MOVIE, name=video_title,
            source=self.ALIASES[0], service_data=service_data
        )]

    def get_titles(self) -> list[Title]:
        if self.is_show:
            return self._get_show_titles()
        else:
            return self._get_single_video_title()

    def _iso_to_seconds(self, duration: str) -> float:
        """Converts an ISO 8601 duration string (e.g., 'PT1H2M3.4S') to seconds."""
        if not duration or not duration.startswith('PT'):
            return 0
        seconds = 0.0
        time_part = duration[2:]
        
        hours_match = re.search(r'(\d+(?:\.\d+)?)H', time_part)
        if hours_match:
            seconds += float(hours_match.group(1)) * 3600
        
        minutes_match = re.search(r'(\d+(?:\.\d+)?)M', time_part)
        if minutes_match:
            seconds += float(minutes_match.group(1)) * 60
            
        seconds_match = re.search(r'(\d+(?:\.\d+)?)S', time_part)
        if seconds_match:
            seconds += float(seconds_match.group(1))

        return seconds

    def _get_subtitles(self, video_info: dict) -> list[TextTrack]:
        subtitles = []
        
        caption_tracks = try_get(video_info, lambda x: x['captions']['playerCaptionsTracklistRenderer']['captionTracks'])
        
        if not caption_tracks:
            web_response = self.page_data.get('ytInitialPlayerResponse', {})
            caption_tracks = try_get(web_response, lambda x: x['captions']['playerCaptionsTracklistRenderer']['captionTracks'])

        if not caption_tracks:
            self.log.info("No captions found in player response.")
            return subtitles

        for track in caption_tracks:
            base_url = track.get('baseUrl')
            if not base_url: continue
            
            name_simple = try_get(track, lambda x: x['name']['simpleText']) or "Unknown"
            language_code = track.get('languageCode', 'und')
            vss_id = track.get('vssId', '')
            is_auto = 'a.' in vss_id or track.get('kind') == 'asr' # Auto-generated
            
            if 'fmt=' not in base_url:
                base_url += '&fmt=vtt'
            
            sub_track = TextTrack(
                id_=f"{self.title_id}_{language_code}_{'auto' if is_auto else 'sub'}",
                source=self.ALIASES[0],
                url=base_url,
                codec="vtt",
                language=language_code,
                sdh=False,
                forced=False,
                note=f"{name_simple} ({'Auto' if is_auto else 'Manual'})"
            )

            sub_track.is_original_lang = not is_auto
            
            subtitles.append(sub_track)
            
        self.log.info(f"Found {len(subtitles)} subtitle tracks.")
        return subtitles

    def get_tracks(self, title: Title) -> Tracks:
        self._initialize_context_and_page_data() 
        
        if 'video_info' not in title.service_data:
            self.log.info(f"Fetching video info for S{title.season:02}E{title.episode:02} (with TV context)...")
            
            context_payload = self._create_context_payload(title.id)

            response = self.session.post(
                f"{self.YOUTUBE_PLAYER_URL}?key={self.API_KEY}", 
                json=context_payload
            )
            response.raise_for_status()
            video_info = response.json()
            
            title.service_data.update(self.context_data)
            title.service_data['video_info'] = video_info
        else:
            video_info = title.service_data['video_info']

        if video_info.get('playabilityStatus', {}).get('status') != "OK":
            reason = video_info.get('playabilityStatus', {}).get('reason', 'Unknown reason')
            self.log.warning(f" - This episode is not playable: {reason}")
            return Tracks()
            
        dash_manifest_url = video_info.get('streamingData', {}).get('dashManifestUrl')
        if not dash_manifest_url: self.log.exit(" - No DASH manifest found.")
        
        final_manifest_data = None
        if video_info.get("adPlacements"):
            self.log.info("Ad placements detected, filtering manifest for main content...")
            try:
                manifest_text = self.session.get(dash_manifest_url).text
                
                ns = {'mpd': 'urn:mpeg:dash:schema:mpd:2011'}
                root = ET.fromstring(manifest_text)
                
                periods = root.findall('mpd:Period', ns)
                
                if len(periods) > 1:
                    main_content_period = None
                    max_duration = -1

                    for period in periods:
                        duration_str = period.get('duration')
                        duration_sec = self._iso_to_seconds(duration_str)
                        if duration_sec > max_duration:
                            max_duration = duration_sec
                            main_content_period = period

                    if main_content_period is not None:
                        self.log.info(f"Found {len(periods)} periods. Selecting main content (duration: {max_duration}s).")
                        for period in periods:
                            if period is not main_content_period:
                                root.remove(period)
                        final_manifest_data = ET.tostring(root, encoding='unicode')
                    else:
                        self.log.warning(" - Could not determine main content period, using original manifest.")
                else:
                    self.log.info("Only one period found in manifest, no filtering needed.")

            except Exception as e:
                self.log.warning(f" - Failed to filter ads from manifest, using original. Reason: {e}")
        else:
            self.log.info("No ad placements found in video info.")
        
        self.log.info("Parsing DASH manifest...")
        tracks = Tracks.from_mpd(
            url=dash_manifest_url, 
            data=final_manifest_data,
            session=self.session, 
            source=self.ALIASES[0]
        )
        
        subtitle_tracks = self._get_subtitles(video_info)
        tracks.add(subtitle_tracks)
        
        return tracks

    def get_chapters(self, title: Title) -> list:
        return []

    def license(self, challenge: bytes, title: Title, track: Any, session_id: str) -> bytes:
        self.log.info("Requesting Widevine license...")
        
        service_data = title.service_data
        video_info = service_data['video_info']
        client_info = self.context_data['client_info'] 
        
        drm_params = video_info.get("streamingData", {}).get("drmParams")

        json_payload = {
            'context': {'client': client_info},
            'drmSystem': 'DRM_SYSTEM_WIDEVINE',
            'videoId': title.id,
            'cpn': 'MsQQaCE9gAkD9iLF',
            'sessionId': service_data.get('session_id', ''),
            'drmParams': drm_params,
            "licenseRequest": base64.b64encode(challenge).decode("utf-8")
        }
        
        lic_response = self.session.post(
            f"{self.LICENSE_SERVER_URL}?key={self.API_KEY}",
            json=json_payload
        )
        lic_response.raise_for_status()
        
        license_b64 = lic_response.json().get("license")
        if not license_b64:
            self.log.exit(f" - License request failed: {lic_response.json()}")

        return base64.b64decode(license_b64.replace("-", "+").replace("_", "/"))