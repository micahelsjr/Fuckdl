from __future__ import annotations

import base64
import hashlib
import json
import uuid
import os
import re
import time
import secrets
import string
from pathlib import Path
from bs4 import BeautifulSoup
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote, urlparse

import click
import jsonpickle
import requests
import random
from langcodes import Language
from tldextract import tldextract
from click.core import ParameterSource

from fuckdl.objects import TextTrack, Title, Tracks, Track
from fuckdl.objects.tracks import MenuTrack
from fuckdl.services.BaseService import BaseService
from fuckdl.utils.Logger import Logger
from fuckdl.utils.widevine.device import BaseDevice, LocalDevice, RemoteDevice

class Amazon(BaseService):
    """
    Service code for Amazon VOD (https://amazon.com) and Amazon Prime Video (https://primevideo.com).

    \b
    Authorization: Cookies
    Security: UHD@L1/SL3000 FHD@L3(ChromeCDM) FHD@L3, Maintains their own license server like Netflix, be cautious.

    \b
    Region is chosen automatically based on domain extension found in cookies.
    Prime Video specific code will be run if the ASIN is detected to be a prime video variant.
    Use 'Amazon Video ASIN Display' for Tampermonkey addon for ASIN
    https://greasyfork.org/en/scripts/381997-amazon-video-asin-display
    
    vt dl --list -z uk -q 1080 Amazon B09SLGYLK8 
    """

    ALIASES = ["AMZN", "amazon"]
    TITLE_RE = [
        r"^(?:https?://(?:www\.)?(?P<domain>amazon\.(?P<region>com|co\.uk|de|co\.jp)|primevideo\.com)(?:/.+)?/)?(?P<id>[A-Z0-9]{10,}|amzn1\.dv\.gti\.[a-f0-9-]+)", r"^(?:https?://(?:www\.)?(?P<domain>amazon\.(?P<region>com|co\.uk|de|co\.jp)|primevideo\.com)(?:/[^?]*)?(?:\?gti=)?)(?P<id>[A-Z0-9]{10,}|amzn1\.dv\.gti\.[a-f0-9-]+)"]

    REGION_TLD_MAP = {
        "au": "com.au",
        "br": "com.br",
        "jp": "co.jp",
        "mx": "com.mx",
        "tr": "com.tr",
        "gb": "co.uk",
        "us": "com",
    }
    VIDEO_RANGE_MAP = {
        "SDR": "None",
        "HDR10": "Hdr10",
        "DV": "DolbyVision",
    }

    @staticmethod
    @click.command(name="Amazon", short_help="https://amazon.com, https://primevideo.com", help=__doc__)
    @click.argument("title", type=str, required=False)
    @click.option("-b", "--bitrate", default="CBR",
                  type=click.Choice(["CVBR", "CBR", "CVBR+CBR"], case_sensitive=False),
                  help="Video Bitrate Mode to download in. CVBR=Constrained Variable Bitrate, CBR=Constant Bitrate.")
    @click.option("-p", "--player", default="html5",
                  type=click.Choice(["html5", "xp"], case_sensitive=False),
                  help="Video playerType to download in. html5, xp.")
    @click.option("-c", "--cdn", default="Akamai", type=str,
                  help="CDN to download from, defaults to the CDN with the highest weight set by Amazon.") # Akamai, Cloudfront
    # UHD, HD, SD. UHD only returns HEVC, ever, even for <=HD only content
    @click.option("-vq", "--vquality", default="HD",
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request.")
    @click.option("-s", "--single", is_flag=True, default=False,
                  help="Force single episode/season instead of getting series ASIN.")
    @click.option("-am", "--amanifest", default="CVBR",
                  type=click.Choice(["CVBR", "CBR", "H265"], case_sensitive=False),
                  help="Manifest to use for audio. Defaults to H265 if the video manifest is missing 640k audio.")
    @click.option("-aq", "--aquality", default="SD",
                  type=click.Choice(["SD", "HD", "UHD"], case_sensitive=False),
                  help="Manifest quality to request for audio. Defaults to the same as --quality.")
    @click.option("-nr", "--no_true_region",is_flag=True, default=False,
                  help="Skip checking true current region.")
    @click.pass_context
    def cli(ctx, **kwargs):
        return Amazon(ctx, **kwargs)

    def __init__(self, ctx, title, bitrate: str, player: str, cdn: str, vquality: str, single: bool,
                 amanifest: str, aquality: str, no_true_region: bool):
        m = self.parse_title(ctx, title)
        self.bitrate = bitrate
        self.player = player
        self.bitrate_source = ctx.get_parameter_source("bitrate")
        self.cdn = cdn
        self.vquality = vquality
        self.vquality_source = ctx.get_parameter_source("vquality")
        self.single = single
        self.amanifest = amanifest
        self.aquality = aquality
        
        self.no_true_region = no_true_region
        
        super().__init__(ctx)

        assert ctx.parent is not None

        self.vcodec = ctx.parent.params["vcodec"] or "H264"
        self.range = ctx.parent.params["range_"] or "SDR"
        self.chapters_only = ctx.parent.params["chapters_only"]
        self.atmos = ctx.parent.params["atmos"]
        self.quality = ctx.parent.params.get("quality") or 1080

        self.cdm = ctx.obj.cdm
        self.profile = ctx.obj.profile
        self.playready = ctx.obj.cdm.device.type == LocalDevice.Types.PLAYREADY

        self.region: dict[str, str] = {}
        self.endpoints: dict[str, str] = {}
        self.device: dict[str, str] = {}

        self.pv = False
        self.rpv = False
        self.event = False
        self.device_token = None
        self.device_id: None
        self.customer_id = None
        self.client_id = "f22dbddb-ef2c-48c5-8876-bed0d47594fd"  # browser client id

        if self.vquality_source != ParameterSource.COMMANDLINE:
            if 0 < self.quality <= 576 and self.range == "SDR":
                self.log.info(" + Setting manifest quality to SD")
                self.vquality = "SD"

            if self.quality > 1080:
                self.log.info(" + Setting manifest quality to UHD to be able to get 2160p video track")
                self.vquality = "UHD"

        self.vquality = self.vquality or "HD"

        if self.vquality == "UHD":
            self.vcodec = "H265"

        if self.bitrate_source != ParameterSource.COMMANDLINE:
            if self.vcodec == "H265" and self.range == "SDR" and self.bitrate != "CVBR+CBR":
                self.bitrate = "CVBR+CBR"
                self.log.info(" + Changed bitrate mode to CVBR+CBR to be able to get H.265 SDR video track")

            if self.vquality == "UHD" and self.range != "SDR" and self.bitrate != "CBR":
                self.bitrate = "CBR"
                self.log.info(f" + Changed bitrate mode to CBR to be able to get highest quality UHD {self.range} video track")

        self.orig_bitrate = self.bitrate

        self.configure()

    # Abstracted functions
    
    def get_titles(self):
        if self.domain == "primevideo" and not self.pv:
            raise self.log.exit("Wrong titleID for primevideo cookies")
        res = self.session.get(
            url=self.endpoints["details"],
            params={
                "titleID": self.title,
                "isElcano": "1",
                "sections": ["Atf", "Btf"]
            },
            headers={
                "Accept": "application/json"
            }
        )

        if not res.ok:
            raise self.log.exit(f"Unable to get title: {res.text} [{res.status_code}]")

        data = res.json()["widgets"]
        product_details = data.get("productDetails", {}).get("detail")

        if not product_details:
            error = res.json()["degradations"][0]
            raise self.log.exit(f"Unable to get title: {error['message']} [{error['code']}]")

        titles = []
        titles_ = []

        if data["pageContext"]["subPageType"] == "Event":
           self.event = True

        if data["pageContext"]["subPageType"] == "Movie" or data["pageContext"]["subPageType"] == "Event":
            card = res["productDetails"]["detail"]
            titles.append(Title(
                id_=card["catalogId"],
                type_=Title.Types.MOVIE,
                name=product_details["title"],
                #year=card["releaseYear"],
                year=card.get("releaseYear", ""),
                # language is obtained afterward
                original_lang=None,
                source=self.ALIASES[0],
                service_data=card
            ))
            playbackEnvelope_info = self.playbackEnvelope_data([card["catalogId"]])
            for title in titles:
                for playbackInfo in playbackEnvelope_info:
                    if title.id == playbackInfo["titleID"]:
                        title.service_data.update({"playbackInfo": playbackInfo})
                        titles_.append(title)
        else:
            if not self.single:
                headers = {
                    "accept": "application/json",
                    "device-memory": "8",
                    "downlink": "10",
                    "dpr": "2",
                    "ect": "4g",
                    "rtt": "50",
                    "viewport-width": "604",
                    "x-amzn-client-ttl-seconds": "58.999",
                    "x-purpose": "navigation",
                    "x-requested-with": "WebSPA",
                }

                if self.pv:
                    res = self.session.get(f"https://{self.region['base']}/detail/{self.title}", headers=headers)
                    if "redirect" in res.text:
                        url = f"https://{self.region['base']}{res.json()['redirect']}"
                    else:
                        url = res.url
                else:
                    url = f"https://{self.region['base']}/dp/{self.title}"
                
                headers.update({"referer": url})

                response = self.session.get(
                    url=url,
                    params={"dvWebSPAClientVersion": "1.0.106799.0"},
                    headers=headers,
                )
                if not response.status_code == 200:
                    raise self.log.exit("Unable to get seasons")
                data = response.json()
                for page in data["page"]:
                    if page:
                        for body in page.get("assembly").get("body"):
                            if body:
                                seasons_data = body.get("props").get("atf").get("state").get("seasons")
                                if seasons_data:
                                    break
                if not seasons_data:
                    raise self.log.exit("Unable to get seasons")
                
                seasons = list(seasons_data.values())[0]
                
                for season in seasons:
                    seasonLink = season["seasonLink"]
                    match = re.search(r"/detail/([A-Z0-9]{10,})/", seasonLink)
                    if match:
                        titleID = match.group(1)
                    else:
                        raise self.log.exit("Unable to get season id")
                    
                    episodes_titles = self.get_episodes(titleID)

                    titles_.extend(episodes_titles)
            else:
                episodes_titles = self.get_episodes(self.title)
                titles_.extend(episodes_titles)
            
        if titles_ == []:
            raise self.log.exit(" - The profile used does not have the rights to this title.")

        if titles_:
            # TODO: Needs playback permission on first title, title needs to be available
            original_lang = self.get_original_language(self.get_manifest(
                next((x for x in titles_ if x.type == Title.Types.MOVIE or x.episode > 0), titles_[0]),
                video_codec=self.vcodec,
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                ignore_errors=True
            ))
            if original_lang:
                for title in titles_:
                    title.original_lang = Language.get(original_lang)
            else:
                #self.log.warning(" - Unable to obtain the title's original language, setting 'en' default...")
                for title in titles_:
                    title.original_lang = Language.get("en")

        filtered_titles = []
        season_episode_count = defaultdict(int)
        for title in titles_:
            key = (title.season, title.episode) 
            if season_episode_count[key] < 1:
                filtered_titles.append(title)
                season_episode_count[key] += 1

        titles = filtered_titles

        return titles

    def get_tracks(self, title: Title) -> Tracks:
        """Modified get_tracks to support HYBRID mode."""
        if self.chapters_only:
            return []

        # Check if HYBRID mode is requested
        hybrid_mode = self.range and self.range.upper() in ("DVHDR", "HDRDV", "HYBRID")
        
        if hybrid_mode:
            # For HYBRID mode, we need both HDR10 and DV tracks
            self.log.info(" + HYBRID mode detected - getting both HDR10 and DV tracks")
            
            # First get HDR10 tracks
            tracks_hdr = self.get_best_quality(title)
            
            # Get HDR10 manifest
            manifest_hdr = self.get_manifest(
                title,
                video_codec=self.vcodec,
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                hdr="HDR10",
                ignore_errors=False
            )
            
            if "rightsException" in manifest_hdr:
                self.log.error(" - The profile used does not have the rights to this title.")
                return
            
            # Get DV manifest for metadata extraction (lowest quality)
            manifest_dv = self.get_manifest(
                title,
                video_codec="H265",
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                hdr="DV",
                ignore_errors=True
            )
            
            if not manifest_dv:
                self.log.warning(" - No DV manifest available for HYBRID mode, falling back to HDR10 only")
                self.range = "HDR10"
                return self.get_tracks(title)  # Recursive call with HDR10
            
            # Process HDR10 manifest
            chosen_manifest_hdr = self.choose_manifest(manifest_hdr, self.cdn)
            if not chosen_manifest_hdr:
                raise self.log.exit(f"No HDR10 manifests available")
            
            manifest_url_hdr = self.clean_mpd_url(chosen_manifest_hdr["url"], False)
            self.log.info(" + Downloading HDR10 Manifest")
            
            streamingProtocol_hdr = manifest_hdr["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
            sessionHandoffToken_hdr = manifest_hdr["sessionization"]["sessionHandoffToken"]
            
            tracks = Tracks()
            
            if streamingProtocol_hdr == "DASH":
                tracks.add(Tracks([
                    x for x in iter(Tracks.from_mpd(
                        url=manifest_url_hdr,
                        session=self.session,
                        source=self.ALIASES[0],
                    ))
                ]))
                for track in tracks:
                    track.extra = track.extra + (sessionHandoffToken_hdr,)
            elif streamingProtocol_hdr == "SmoothStreaming":
                tracks.add(Tracks([
                    x for x in iter(Tracks.from_ism(
                        url=manifest_url_hdr,
                        source=self.ALIASES[0],
                    ))
                ]))
                for track in tracks:
                    track.extra = track.extra + (sessionHandoffToken_hdr,)
            
            # Mark HDR10 videos
            for video in tracks.videos:
                video.hdr10 = True
                video.dv = False
            
            # Process DV manifest (get lowest quality for metadata)
            chosen_manifest_dv = self.choose_manifest(manifest_dv, self.cdn)
            if chosen_manifest_dv:
                manifest_url_dv = self.clean_mpd_url(chosen_manifest_dv["url"], False)
                self.log.info(" + Downloading DV Manifest (for metadata)")
                
                streamingProtocol_dv = manifest_dv["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                sessionHandoffToken_dv = manifest_dv["sessionization"]["sessionHandoffToken"]
                
                if streamingProtocol_dv == "DASH":
                    dv_tracks = Tracks([
                        x for x in iter(Tracks.from_mpd(
                            url=manifest_url_dv,
                            session=self.session,
                            source=self.ALIASES[0],
                        ))
                    ])
                    for track in dv_tracks:
                        track.extra = track.extra + (sessionHandoffToken_dv,)
                elif streamingProtocol_dv == "SmoothStreaming":
                    dv_tracks = Tracks([
                        x for x in iter(Tracks.from_ism(
                            url=manifest_url_dv,
                            source=self.ALIASES[0],
                        ))
                    ])
                    for track in dv_tracks:
                        track.extra = track.extra + (sessionHandoffToken_dv,)
                
                # Mark DV videos and add lowest quality DV track
                for video in dv_tracks.videos:
                    video.dv = True
                    video.hdr10 = False
                
                # Sort DV tracks by bitrate and add the lowest one
                dv_tracks.videos = sorted(dv_tracks.videos, key=lambda x: float(x.bitrate or 0.0))
                if dv_tracks.videos:
                    tracks.add([dv_tracks.videos[0]], warn_only=True)
                    self.log.info(f" + Added DV track for metadata: {dv_tracks.videos[0].bitrate // 1000 if dv_tracks.videos[0].bitrate else '?'} kb/s")
        else:
            # Normal (non-HYBRID) mode - use existing logic
            tracks = self.get_best_quality(title)
            
            manifest = self.get_manifest(
                title,
                video_codec=self.vcodec,
                bitrate_mode=self.bitrate,
                quality=self.vquality,
                hdr=self.range,
                ignore_errors=False
            )
            
            if "rightsException" in manifest:
                self.log.error(" - The profile used does not have the rights to this title.")
                return
            
            chosen_manifest = self.choose_manifest(manifest, self.cdn)
            if not chosen_manifest:
                raise self.log.exit(f"No manifests available")
            
            manifest_url = self.clean_mpd_url(chosen_manifest["url"], False)
            if self.event:
                devicetype = self.device["device_type"]
                manifest_url = chosen_manifest["url"]
                manifest_url = f"{manifest_url}?amznDtid={devicetype}&encoding=segmentBase"
            
            self.log.info(" + Downloading Manifest")
            
            streamingProtocol = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
            sessionHandoffToken = manifest["sessionization"]["sessionHandoffToken"]
            
            if streamingProtocol == "DASH":
                tracks.add(Tracks([
                    x for x in iter(Tracks.from_mpd(
                        url=manifest_url,
                        session=self.session,
                        source=self.ALIASES[0],
                    ))
                ]))
                for track in tracks:
                    track.extra = track.extra + (sessionHandoffToken,)
            elif streamingProtocol == "SmoothStreaming":
                tracks.add(Tracks([
                    x for x in iter(Tracks.from_ism(
                        url=manifest_url,
                        source=self.ALIASES[0],
                    ))
                ]))
                for track in tracks:
                    track.extra = track.extra + (sessionHandoffToken,)
            else:
                raise self.log.exit(f"Unsupported manifest type: {streamingProtocol}")
            
            for video in tracks.videos:
                video.hdr10 = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["dynamicRange"] == "Hdr10"
                video.dv = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["dynamicRange"] == "DolbyVision"

        # Continue with audio/subtitle processing (same for both HYBRID and normal modes)
        need_separate_audio = ((self.aquality or self.vquality) != self.vquality
                               or self.amanifest == "CVBR" and (self.vcodec, self.bitrate) != ("H264", "CVBR")
                               or self.amanifest == "CBR" and (self.vcodec, self.bitrate) != ("H264", "CBR")
                               or self.amanifest == "H265" and self.vcodec != "H265"
                               or self.amanifest != "H265" and self.vcodec == "H265")

        if not need_separate_audio:
            audios = defaultdict(list)
            for audio in tracks.audios:
                audios[audio.language].append(audio)

            for lang in audios:
                if not any((x.bitrate or 0) >= 640000 for x in audios[lang]):
                    need_separate_audio = True
                    break

        # If we need separate audio manifests (or user requested --atmos),
        # try fetching higher-bitrate audio manifests (e.g. CVBR/H265) so
        # that when --atmos is used we can still obtain non-Atmos audio
        # tracks at 640kbps if available.
        if need_separate_audio or self.atmos:
            manifest_type = self.amanifest or "CVBR"
            self.log.info(f"Getting audio from {manifest_type} manifest for potential higher bitrate or better codec")
            audio_manifest = self.get_manifest(
                title=title,
                video_codec="H265" if manifest_type == "H265" else "H264",
                bitrate_mode="CVBR",
                quality=self.aquality or self.vquality,
                hdr=None,
                ignore_errors=True
            )
            if not audio_manifest:
                self.log.warning(f" - Unable to get {manifest_type} audio manifests, skipping")
            elif not (chosen_audio_manifest := self.choose_manifest(audio_manifest, self.cdn)):
                self.log.warning(f" - No {manifest_type} audio manifests available, skipping")
            else:
                audio_mpd_url = self.clean_mpd_url(chosen_audio_manifest["url"], optimise=False)
                self.log.debug(audio_mpd_url)
                if self.event:
                    devicetype = self.device["device_type"]
                    audio_mpd_url = chosen_audio_manifest["url"]
                    audio_mpd_url = f"{audio_mpd_url}?amznDtid={devicetype}&encoding=segmentBase"
                self.log.info(" + Downloading CVBR manifest")

                streamingProtocol = audio_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                sessionHandoffToken = audio_manifest["sessionization"]["sessionHandoffToken"]

                try:
                    if streamingProtocol == "DASH":
                        audio_mpd = Tracks([
                                x for x in iter(Tracks.from_mpd(
                                url=audio_mpd_url,
                                session=self.session,
                                source=self.ALIASES[0],
                            ))
                        ])
                        for track in audio_mpd:
                            track.extra = track.extra + (sessionHandoffToken,)
                    elif streamingProtocol == "SmoothStreaming":
                        audio_mpd = Tracks([
                                x for x in iter(Tracks.from_ism(
                                url=audio_mpd_url,
                                source=self.ALIASES[0],
                            ))
                        ])
                        for track in audio_mpd:
                            track.extra = track.extra + (sessionHandoffToken,)
                except KeyError:
                    self.log.warning(f" - Title has no {self.amanifest} stream, cannot get higher quality audio")
                else:
                    tracks.add(audio_mpd.audios, warn_only=True)

        # UHD audio handling remains the same
        need_uhd_audio = self.atmos

        if not self.amanifest and ((self.aquality == "UHD" and self.vquality != "UHD") or not self.aquality):
            audios = defaultdict(list)
            for audio in tracks.audios:
                audios[audio.language].append(audio)
            for lang in audios:
                if not any((x.bitrate or 0) >= 640000 for x in audios[lang]):
                    need_uhd_audio = True
                    break

        if need_uhd_audio and (self.config.get("device") or {}).get(self.profile, None):
            self.log.info("Getting audio from UHD manifest for potential higher bitrate or better codec")
            temp_device = self.device
            temp_device_token = self.device_token
            temp_device_id = self.device_id
            uhd_audio_manifest = None

            try:
                if self.cdm.device.type in [LocalDevice.Types.CHROME, LocalDevice.Types.PLAYREADY] and self.quality < 2160:
                    self.log.info(f" + Switching to device to get UHD manifest")
                    self.register_device()

                uhd_audio_manifest = self.get_manifest(
                    title=title,
                    video_codec="H265",
                    bitrate_mode="CVBR+CBR",
                    quality="UHD",
                    hdr="DV",
                    ignore_errors=True
                )
            except:
                pass

            self.device = temp_device
            self.device_token = temp_device_token
            self.device_id = temp_device_id

            if not uhd_audio_manifest:
                self.log.warning(f" - Unable to get UHD manifests, skipping")
            elif not (chosen_uhd_audio_manifest := self.choose_manifest(uhd_audio_manifest, self.cdn)):
                self.log.warning(f" - No UHD manifests available, skipping")
            else:
                uhd_audio_mpd_url = self.clean_mpd_url(chosen_uhd_audio_manifest["url"], optimise=False)
                self.log.debug(uhd_audio_mpd_url)
                if self.event:
                    devicetype = self.device["device_type"]
                    uhd_audio_mpd_url = chosen_uhd_audio_manifest["url"]
                    uhd_audio_mpd_url = f"{uhd_audio_mpd_url}?amznDtid={devicetype}&encoding=segmentBase"
                self.log.info(" + Downloading UHD manifest")

                streamingProtocol = uhd_audio_manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
                sessionHandoffToken = uhd_audio_manifest["sessionization"]["sessionHandoffToken"]

                try:
                    if streamingProtocol == "DASH":
                        uhd_audio_mpd = Tracks([
                                x for x in iter(Tracks.from_mpd(
                                url=uhd_audio_mpd_url,
                                session=self.session,
                                source=self.ALIASES[0],
                            ))
                        ])
                        for track in uhd_audio_mpd:
                            track.extra = track.extra + (sessionHandoffToken,)
                    elif streamingProtocol == "SmoothStreaming":
                        uhd_audio_mpd = Tracks([
                                x for x in iter(Tracks.from_ism(
                                url=uhd_audio_mpd_url,
                                source=self.ALIASES[0],
                            ))
                        ])
                        for track in uhd_audio_mpd:
                            track.extra = track.extra + (sessionHandoffToken,)
                except KeyError:
                    self.log.warning(f" - Title has no UHD stream, cannot get higher quality audio")
                else:
                    if any(x for x in uhd_audio_mpd.audios if x.atmos):
                        # Instead of replacing all audio tracks with the UHD manifest's
                        # tracks (which may only contain Atmos), merge Atmos tracks with
                        # existing audio tracks so we keep other high-bitrate (640kbps)
                        # audio tracks (added above) while still including Atmos.
                        atmos_tracks = [x for x in uhd_audio_mpd.audios if x.atmos]
                        # Start with existing audios and add Atmos tracks if missing
                        existing = list(tracks.audios)
                        for a in atmos_tracks:
                            # avoid exact object duplicates
                            if not any((ea.language == a.language and (ea.bitrate or 0) == (a.bitrate or 0) and getattr(ea, 'atmos', False) == getattr(a, 'atmos', False)) for ea in existing):
                                existing.append(a)
                        tracks.audios = existing

        # Audio metadata processing
        for audio in tracks.audios:
            if audio.descriptor == audio.descriptor.MPD:
                audio.descriptive = audio.extra[1].get("audioTrackSubtype") == "descriptive"
                audio_track_id = audio.extra[1].get("audioTrackId")
                if audio_track_id:
                    audio.language = Language.get(audio_track_id.split("_")[0])
                if audio.extra[1] is not None and "boosteddialog" in audio.extra[1].get("audioTrackSubtype", ""):
                    audio.bitrate = 1
            elif audio.descriptor == audio.descriptor.ISM:
                audio.descriptive = audio.extra[0].get("audioTrackSubtype") == "descriptive"
                audio_track_id = audio.extra[0].get("audioTrackId")
                if audio_track_id:
                    audio.language = Language.get(audio_track_id.split("_")[0])
                if audio.extra[1] is not None and "boosteddialog" in audio.extra[1].get("audioTrackSubtype", ""):
                    audio.bitrate = 1
                    
        # Remove duplicate audio tracks
        unique_audio_tracks = {}
        for audio in tracks.audios:
            key = (audio.language, audio.bitrate, audio.descriptive)
            if key not in unique_audio_tracks:
                unique_audio_tracks[key] = audio
        tracks.audios = list(unique_audio_tracks.values())

        # If user requested Atmos, prefer including a non-Atmos audio track
        # at >=640 kb/s per language (if available), while still including
        # Atmos tracks. This ensures we don't end up with only a low-bitrate
        # Atmos track and miss higher-bitrate non-Atmos alternatives.
        if self.atmos and tracks.audios:
            from collections import defaultdict as _dd
            grouped = _dd(list)
            for a in tracks.audios:
                grouped[a.language].append(a)

            selected = []
            for lang, group in grouped.items():
                # find non-atmos candidates >=640kbps
                non_atmos_high = [x for x in group if not getattr(x, 'atmos', False) and (getattr(x, 'bitrate', 0) or 0) >= 640000]
                if non_atmos_high:
                    best_non_atmos = max(non_atmos_high, key=lambda x: getattr(x, 'bitrate', 0) or 0)
                else:
                    non_atmos_any = [x for x in group if not getattr(x, 'atmos', False)]
                    best_non_atmos = max(non_atmos_any, key=lambda x: getattr(x, 'bitrate', 0) or 0) if non_atmos_any else None

                # include best non-atmos (if any)
                if best_non_atmos:
                    selected.append(best_non_atmos)

                # include all atmos tracks for this language (avoid duplicates)
                for a in group:
                    if getattr(a, 'atmos', False):
                        if not any((sa.language == a.language and (sa.bitrate or 0) == (a.bitrate or 0) and getattr(sa, 'atmos', False) == getattr(a, 'atmos', False)) for sa in selected):
                            selected.append(a)

            if selected:
                tracks.audios = selected
        
        # Subtitle processing
        if not hybrid_mode:
            # Only get subtitles once in non-HYBRID mode
            manifest_for_subs = manifest if not hybrid_mode else manifest_hdr
            for sub in manifest_for_subs.get("timedTextUrls", {}).get("result", {}).get("subtitleUrls", []) + \
                       manifest_for_subs.get("timedTextUrls", {}).get("result", {}).get("forcedNarrativeUrls", []):
                tracks.add(TextTrack(
                    id_=f"{sub['trackGroupId']}_{sub['languageCode']}_{sub['type']}_{sub['subtype']}",
                    source=self.ALIASES[0],
                    url=os.path.splitext(sub["url"])[0] + ".srt",
                    codec="srt",
                    language=sub["languageCode"],
                    forced="ForcedNarrative" in sub["type"],
                    sdh=sub["type"].lower() == "sdh"
                ), warn_only=True)
        
        for track in tracks:
            track.needs_proxy = False

        # Session management
        if self.vquality != "UHD" and not self.no_true_region:
            self.manage_session(tracks.videos[0])

        return tracks

    def get_chapters(self, title: Title) -> list[MenuTrack]:
        """Get chapters from Amazon's XRay Scenes API."""

        # Too Old endpoint for security reasons don't return chapters
        return []

        manifest = self.get_manifest(
            title,
            video_codec=self.vcodec,
            bitrate_mode=self.bitrate,
            quality=self.vquality,
            hdr=self.range
        )

        if "vodXrayMetadata" in manifest:
            if "error" in manifest["vodXrayMetadata"]:
                self.log.warn(f" - Unable to get chapters: {manifest['vodXrayMetadata']['error']['code']}, {manifest['vodXrayMetadata']['error']['message']}")
                return []
            xray_params = {
                "pageId": "fullScreen",
                "pageType": "xray",
                "serviceToken": json.dumps({
                    "consumptionType": "Streaming",
                    "deviceClass": "normal",
                    "playbackMode": "playback",
                    "vcid": json.loads(manifest["vodXrayMetadata"]["result"]["parameters"]["serviceToken"])["vcid"]
                })
            }
        else:
            return []

        xray_params.update({
            "deviceID": self.device_id,
            "deviceTypeID": self.config["device_types"]["browser"],  # must be browser device type
            "marketplaceID": self.region["marketplace_id"],
            "gascEnabled": str(self.pv).lower(),
            "decorationScheme": "none",
            "version": "inception-v2",
            "uxLocale": "en-US",
            "featureScheme": "XRAY_WEB_2020_V1"
        })

        xray = self.session.get(
            url=self.endpoints["xray"],
            params=xray_params
        ).json().get("page")

        if not xray:
            return []

        widgets = xray["sections"]["center"]["widgets"]["widgetList"]

        scenes = next((x for x in widgets if x["tabType"] == "scenesTab"), None)
        if not scenes:
            return []
        scenes = scenes["widgets"]["widgetList"][0]["items"]["itemList"]

        chapters = []

        for scene in scenes:
            chapter_title = scene["textMap"]["PRIMARY"]
            match = re.search(r"(\d+\. |)(.+)", chapter_title)
            if match:
                chapter_title = match.group(2)
            chapters.append(MenuTrack(
                number=int(scene["id"].replace("/xray/scene/", "")),
                title=chapter_title,
                timecode=scene["textMap"]["TERTIARY"].replace("Starts at ", "")
            ))

        return chapters

    def certificate(self, **_):
        return self.config["certificate"]
        
    def license(self, challenge: bytes, title: Title, track: Track, *_, **__) -> Union[bytes, str, dict, None]:
        if (
            isinstance(self.cdm, (RemoteDevice, LocalDevice))
            and challenge != self.cdm.service_certificate_challenge
        ):
            self.register_device(
                quality=track.quality or getattr(track, "height", None)
            )

        if self.playready:
            license_type_key = "playReadyLicense"
            license_endpoint = "license_pr"
            other_params = {}
        else:
            license_type_key = "widevineLicense"
            license_endpoint = "license_wv"
            other_params = {
                "includeHdcpTestKey": True,
            }

        # Build request payload
        request_json = {
            **other_params,
            "licenseChallenge": base64.b64encode(challenge).decode(),
            "playbackEnvelope": self.playbackInfo["playbackExperienceMetadata"]["playbackEnvelope"],
        }

# Add device-specific parameters
        if self.device_token:
            # Android/device-based flow (for both Widevine and PlayReady with device)
            request_json.update({
                "capabilityDiscriminators": {
                    "discriminators": {
                        "hardware": {
                            "chipset": self.device["device_chipset"],
                            "manufacturer": self.device["manufacturer"],
                            "modelName": self.device["device_model"],
                        },
                        "software": {
                            "application": {
                                "name": self.device["app_name"],
                                "version": self.device["firmware"],
                            },
                            "client": {
                                "id": None,
                            },
                            **(
                                {
                                    "firmware": {
                                        "version": str(
                                            self.device["firmware_version"]
                                        ),
                                    },
                                }
                                if self.device.get("firmware_version")
                                else {}
                            ),
                            "operatingSystem": {
                                "name": "Android",
                                "version": self.device["os_version"],
                            },
                            "player": {
                                "name": "Android UIPlayer SDK",
                                "version": "4.1.18",
                            },
                            "renderer": {
                                "drmScheme": "WIDEVINE" if not self.playready else "PLAYREADY",
                                "name": "MCMD",
                            },
                        },
                    },
                    "version": 1,
                },
                "deviceCapabilityFamily": "AndroidPlayer",
                "keyId": str(uuid.UUID(track.kid)).upper(),
                "packagingFormat": "SMOOTH_STREAMING"
                if track.descriptor == Track.Descriptor.ISM
                else "MPEG_DASH",
            })
        else:
            # Web-based flow - needs sessionHandoff from track.extra (Widevine only)
            # The sessionHandoffToken is stored in track.extra[2] during get_tracks()
            try:
                session_handoff = track.extra[2] if len(track.extra) > 2 else None
            except (IndexError, AttributeError):
                session_handoff = None
            
            if not session_handoff:
                self.log.exit("No sessionHandoff found in track data. Web licensing requires sessionHandoff.")
            
            request_json.update({
                "sessionHandoff": session_handoff,
                "deviceCapabilityFamily": "WebPlayer",
            })

        try:
            res = self.session.post(
                url=self.endpoints[license_endpoint],
                headers={
                    "accept": "application/json",
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {self.device_token}"
                    if self.device_token
                    else None,
                    "connection": "Keep-Alive",
                    "x-gasc-enabled": "true",
                    "x-request-priority": "CRITICAL",
                    "x-retry-count": "0",
                    "nerid": self.generate_nerid(),
                },
                params={
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": "1",
                    "titleId": title.id,
                    "nerid": self.generate_nerid(),
                },
                json=request_json,
            )
            res.raise_for_status()
            response_data = res.json()
        except requests.exceptions.HTTPError as e:
            msg = "Failed to license"
            if e.response is not None:
                try:
                    res_json = e.response.json()
                    msg += f": {res_json}"
                except Exception:
                    msg += f": {e.response.text}"
            else:
                msg += f": {str(e)}"
            
            self.log.exit(msg)
        except Exception as e:
            self.log.exit(f"Failed to license: {str(e)}")

        return response_data[license_type_key]["license"]
        
        
    
    def configure(self) -> None:
        if len(self.title) > 10:
            self.pv = True

        self.log.info("Getting Account Region")
        self.region = self.get_region()
        if not self.region:
            raise self.log.exit(" - Failed to get Amazon Account region")
        self.GEOFENCE.append(self.region["code"])
        
        if self.no_true_region:
            self.log.info(f" + Region: {self.region['code']}")

        # endpoints must be prepared AFTER region data is retrieved
        self.endpoints = self.prepare_endpoints(self.config["endpoints"], self.region)

        self.session.headers.update({
            "Origin": f"https://{self.region['base']}",
            "Referer": f"https://{self.region['base']}/"
        })

        self.device = (self.config.get("device") or {}).get(self.profile, {})
        if self.device and self.device["device_type"] not in set(self.config["dtid_dict"]):
            raise self.log.exit(f"{self.device['device_type']} Banned from Amazon Prime, Use another one to avoid Amazon Account Ban !!!")
        if (self.quality > 1080 or self.range != "SDR") and self.vcodec == "H265" and self.cdm.device.type == LocalDevice.Types.CHROME:
            self.log.info(f"Using device to get UHD manifests")
            self.register_device()
        elif not self.device or self.cdm.device.type == LocalDevice.Types.CHROME or self.vquality != "UHD":
            # falling back to browser-based device ID
            if not self.device:
                self.log.warning(
                    "No Device information was provided for %s, using browser device...",
                    self.profile
                )
            self.device_id = "c3714f0d-59c9-4eb7-8b96-903f0f8c3619" #hashlib.sha224(
                #("CustomerID" + self.session.headers["User-Agent"]).encode("utf-8")
            #).hexdigest()
            self.device = {"device_type": self.config["device_types"]["browser"]}

            res = self.session.get(
                url=self.endpoints["configuration"],
                params = {
                    "deviceTypeID": self.device["device_type"],
                    "deviceID": "Web",
                }
            )

            if not res.status_code == 200:
                raise self.log.exit(res.text)
            
            data = res.json()
            
            #PK added if
            if not self.no_true_region:
                self.log.info(f" + Current Region: {data['requestContext']['currentTerritory']}")     
                self.region["marketplace_id"] = data["requestContext"]["marketplaceID"]
            
        else:
            res = self.session.get(
                url=self.endpoints["configuration"],
                params = {
                    "deviceTypeID": self.device["device_type"],
                    "deviceID": "Tv",
                }
            )

            if not res.status_code == 200:
                raise self.log.exit(res.text)
            
            data = res.json()
            
            #PK added if
            if not self.no_true_region:
                self.log.info(f" + Current Region: {data['requestContext']['currentTerritory']}")
                self.region["marketplace_id"] = data["requestContext"]["marketplaceID"]

            self.register_device()

    def register_device(self) -> None:
        self.device = (self.config.get("device") or {}).get(self.profile, {})
        device_cache_path = self.get_cache("device_tokens_{profile}_{hash}.json".format(
            profile=self.profile,
            hash=hashlib.md5(json.dumps(self.device).encode()).hexdigest()[0:6]
        ))
        self.device_token = self.DeviceRegistration(
            device=self.device,
            endpoints=self.endpoints,
            log=self.log,
            cache_path=device_cache_path,
            session=self.session
        ).bearer
        self.device_id = self.device.get("device_serial")
        if not self.device_id:
            raise self.log.exit(f" - A device serial is required in the config, perhaps use: {os.urandom(8).hex()}")

    def get_region(self) -> dict:
        domain_region = self.get_domain_region()
        if not domain_region:
            return {}

        region = self.config["regions"].get(domain_region)
        if not region:
            raise self.log.exit(f" - There's no region configuration data for the region: {domain_region}")

        region["code"] = domain_region

        if self.pv:
            res = self.session.get("https://www.primevideo.com").text
            soup = BeautifulSoup(res, 'html.parser')
            scripts = soup.find_all('script')
            pv_url = None
            for script in scripts:
                if script.string:
                    if 'DVWebNode.loggingEndpoint' in script.string:
                        match = re.search(r"DVWebNode\.loggingEndpoint\s*=\s*'([^']+)';", script.string)
                        if match:
                            pv_url = match.group(1)
                            break
            if pv_url is None:
                raise self.log.exit(" - Failed to get PrimeVideo region")
            try:
                parsed = urlparse(pv_url)
                baseUrl = parsed.netloc
            except Exception as e:
                raise self.log.exit(f" - Failed to get PrimeVideo region: {e}")
            #pv_region = {"na": "atv-ps"}.get(pv_region, f"atv-ps-{pv_region}")
            region["base_manifest"] = baseUrl #f"{pv_region}.primevideo.com"
            region["base"] = "www.primevideo.com"

        return region

    def get_domain_region(self):
        """Get the region of the cookies from the domain."""
        tlds = [tldextract.extract(x.domain) for x in self.cookies if x.domain_specified]
        tld = next((x.suffix for x in tlds if x.domain.lower() in ("amazon", "primevideo")), None)
        self.domain = next((x.domain for x in tlds if x.domain.lower() in ("amazon", "primevideo")), None).lower()
        if tld:
            tld = tld.split(".")[-1]
        return {"com": "us", "uk": "gb"}.get(tld, tld)

    def prepare_endpoint(self, name: str, uri: str, region: dict) -> str:
        if name in ("browse", "configuration", "refreshplayback", "playback", "license_wv", "license_pr", "xray", "opensession", "updatesession", "closesession"):
            return f"https://{(region['base_manifest'])}{uri}"
        if name in ("ontv", "devicelink", "details", "getDetailWidgets", "metadata"):
            if self.pv:
                host = "www.primevideo.com"
            else:
                if name in ("metadata"):
                    host = f"{region['base']}/gp/video"
                else:
                    host = region["base"]
            return f"https://{host}{uri}"
        if name in ("codepair", "register", "token"):
            return f"https://{self.config['regions']['us']['base_api']}{uri}"
        raise ValueError(f"Unknown endpoint: {name}")

    def prepare_endpoints(self, endpoints: dict, region: dict) -> dict:
        return {k: self.prepare_endpoint(k, v, region) for k, v in endpoints.items()}

    def choose_manifest(self, manifest: dict, cdn=None):
        """Get manifest URL for the title based on CDN weight (or specified CDN)."""
        if cdn:
            cdn = cdn.lower()
            manifest = next((x for x in manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlSets"] if x["cdn"].lower() == cdn), {})
            if not manifest:
                raise self.log.exit(f" - There isn't any DASH manifests available on the CDN \"{cdn}\" for this title")
        else:
            url_sets = manifest["vodPlaybackUrls"]["result"]["playbackUrls"].get("urlSets", [])
            manifest = random.choice(url_sets) if url_sets else {}

        return manifest
    
    def manage_session(self, track: Tracks):
        try:
            current_progress_time = round(random.uniform(0, 10), 6)
            time_ = 3 # Seconds

            # Open Session
            stream_update_time = datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
            res = self.session.post(
                url=self.endpoints["opensession"],
                params={
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": "1",
                    "version": "1",
                    "nerid": self.generate_nerid(),
                },
                headers={
                    "Content-Type": "application/json",
                    "accept": "application/json",
                    "x-request-priority": "CRITICAL",
                    "x-retry-count": "0"
                },
                json={
                    "sessionHandoff": track.extra[2],
                    "playbackEnvelope": self.playbackEnvelope_update(self.playbackInfo)["playbackExperienceMetadata"]["playbackEnvelope"],
                    "streamInfo": {
                        "eventType": "START",
                        "streamUpdateTime": current_progress_time,
                        "vodProgressInfo": {
                            "currentProgressTime": f"PT{current_progress_time:.6f}S",
                            "timeFormat": "ISO8601DURATION",
                        },
                    },
                    "userWatchSessionId": str(uuid.uuid4())
                }
            )
            if res.status_code == 200:
                try:
                    data = res.json()
                    sessionToken = data["sessionToken"]
                except Exception as e:
                    raise self.log.exit(f"Unable to open session: {e}")
            else:
                raise self.log.exit(f"Unable to open session: {res.text}")
            
            # Update Session
            time.sleep(time_)
            stream_update_time = (datetime.fromisoformat(stream_update_time[:-1]) + timedelta(seconds=time_)).isoformat(timespec="milliseconds") + "Z"
            res = self.session.post(
                url=self.endpoints["updatesession"],
                params={
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": "1",
                    "version": "1",
                    "nerid": self.generate_nerid()
                },
                headers={
                    "Content-Type": "application/json",
                    "accept": "application/json",
                    "x-request-priority": "CRITICAL",
                    "x-retry-count": "0"
                },
                json={
                    "sessionToken": sessionToken,
                    "streamInfo": {
                        "eventType": "PAUSE",
                        "streamUpdateTime": stream_update_time,
                        "vodProgressInfo": {
                            "currentProgressTime": f"PT{current_progress_time + time_:.6f}S",
                            "timeFormat": "ISO8601DURATION",
                        }
                    }
                }
            )
            if res.status_code == 200:
                try:
                    data = res.json()
                    sessionToken = data["sessionToken"]
                except Exception as e:
                    raise self.log.exit(f"Unable to update session: {e}")
            else:
                raise self.log.exit(f"Unable to update session: {res.text}")
            
            # Close session
            res = self.session.post(
                url=self.endpoints["closesession"],
                params={
                    "deviceID": self.device_id,
                    "deviceTypeID": self.device["device_type"],
                    "gascEnabled": str(self.pv).lower(),
                    "marketplaceID": self.region["marketplace_id"],
                    "uxLocale": "en_EN",
                    "firmware": "1",
                    "version": "1",
                    "nerid": self.generate_nerid()
                },
                headers={
                    "Content-Type": "application/json",
                    "accept": "application/json",
                    "x-request-priority": "CRITICAL",
                    "x-retry-count": "0"
                },
                json={
                    "sessionToken": sessionToken,
                    "streamInfo": {
                        "eventType": "STOP",
                        "streamUpdateTime": stream_update_time,
                        "vodProgressInfo": {
                            "currentProgressTime": f"PT{current_progress_time + time_:.6f}S",
                            "timeFormat": "ISO8601DURATION",
                        }
                    }
                }
            )
            if res.status_code == 200:
                self.log.info("Session completed successfully!")
                return None
            else:
                raise self.log.exit(f"Unable to close session: {res.text}")
        except Exception as e:
            raise self.log.exit(f"Unable to get session: {e}")

    def playbackEnvelope_data(self, titles):
        try:
            res = self.session.get(
                url=self.endpoints["metadata"],
                params={
                    "metadataToEnrich": json.dumps({"placement": "HOVER", "playback": "true", "preroll": "true", "trailer": "true", "watchlist": "true"}),
                    "titleIDsToEnrich": json.dumps(titles),
                    "currentUrl":  f"https://{self.region['base']}/"
                },
                headers={
                    "device-memory": "8",
                    "downlink": "10",
                    "dpr": "2",
                    "ect": "4g",
                    "rtt": "50",
                    "viewport-width": "671",
                    "x-amzn-client-ttl-seconds": "15",
                    "x-amzn-requestid": "".join(random.choices(string.ascii_uppercase + string.digits, k=20)).upper(),
                    "x-requested-with": "XMLHttpRequest"
                }
            )
            
            if res.status_code == 200:
                try:
                    data = res.json()
                    playbackEnvelope_info = []
                    enrichments = data["enrichments"]
                    
                    for titleid_, enrichment in list(enrichments.items()):
                        playbackActions = enrichment["playbackActions"]
                        if enrichment["entitlementCues"]['focusMessage'].get('message') == "Watch with a 30 day free Prime trial, auto renews at €4.99/month":
                            raise self.log.exit("Cookies Expired")
                        if playbackActions == []:
                            continue
                            #raise self.log.exit(" - The profile used does not have the rights to this title.")
                        for playbackAction in playbackActions:
                            if playbackAction.get("titleID") or playbackAction.get("legacyOfferASIN"):
                                title_id = titleid_ #playbackAction.get("titleID")
                                playbackExperienceMetadata = playbackAction.get("playbackExperienceMetadata")
                                if not title_id or not playbackExperienceMetadata:
                                    continue
                                    #raise self.log.exit("Unable to get playbackEnvelope informations")
                                playbackEnvelope_info.append({"titleID": title_id, "playbackExperienceMetadata": playbackExperienceMetadata})
                    return playbackEnvelope_info
                except Exception as e:             
                    raise self.log.exit(f"Unable to get playbackEnvelope: {e}")
            else:
                return []
                #raise self.log.exit(f"Unable to get playbackEnvelope: {res.text}")
        except Exception as e:
            return []
            #raise self.log.exit(f"Unable to get playbackEnvelope: {e}")
        
    def playbackEnvelope_update(self, playbackInfo):
        try:
            if not playbackInfo:
                self.log.exit("Unable to update playbackEnvelope")
            if (int(playbackInfo["playbackExperienceMetadata"]["expiryTime"]) / 1000) < time.time():
                self.log.warn("Updating playbackEnvelope")
                correlationId = playbackInfo["playbackExperienceMetadata"]["correlationId"]
                titleID = playbackInfo["titleID"]
                res = self.session.post(
                    url=self.endpoints["refreshplayback"],
                    params={
                        "deviceID": self.device_id,
                        "deviceTypeID": self.device["device_type"],
                        "gascEnabled": str(self.pv).lower(),
                        "marketplaceID": self.region["marketplace_id"],
                        "uxLocale": "en_EN",
                        "firmware": "1",
                        "version": "1",
                        "nerid": self.generate_nerid()
                    },
                    data=json.dumps({
                        "deviceId": self.device_id, 
                        "deviceTypeId": self.device["device_type"],
                        "identifiers": {titleID: correlationId},
                        "geoToken": "null",
                        "identityContext": "null"
                    })
                )
                if res.status_code == 200:
                    try:
                        data = res.json()
                        playbackExperience = data["response"][titleID]["playbackExperience"]
                        playbackExperience["expiryTime"] = int(playbackExperience["expiryTime"] * 1000)
                        return {"titleID": titleID, "playbackExperienceMetadata": playbackExperience}
                    except Exception as e:
                        raise self.log.exit(f"Unable to update playbackEnvelope: {e}")
                else:
                    raise self.log.exit(f"Unable to update playbackEnvelope: {res.text}")
            else:
                return playbackInfo
        except Exception as e:
            raise self.log.exit(f"Unable to update playbackEnvelope {e}")

    def get_manifest(
        self, title: Title, video_codec: str, bitrate_mode: str, quality: str, hdr=None,
            ignore_errors: bool = False
    ) -> dict:
        self.playbackInfo = self.playbackEnvelope_update(title.service_data.get("playbackInfo"))
        title.service_data["playbackInfo"] = self.playbackInfo
        data_dict = {
            "globalParameters": {
                "deviceCapabilityFamily": "WebPlayer" if not self.device_token else "AndroidPlayer",
                "playbackEnvelope": self.playbackInfo["playbackExperienceMetadata"]["playbackEnvelope"],
                "capabilityDiscriminators": {
                    "operatingSystem": {"name": "Windows", "version": "10.0"},
                    "middleware": {"name": "EdgeNext", "version": "136.0.0.0"},
                    "nativeApplication": {"name": "EdgeNext", "version": "136.0.0.0"},
                    "hfrControlMode": "Legacy",
                    "displayResolution": {"height": 2304, "width": 4096}
                } if not self.device_token else {
                    "discriminators": {
                        "software": {},
                        "version": 1
                    }
                }
            },
            "auditPingsRequest": {
                **({
                    "device": {
                        "category": "Tv",
                        "platform": "Android"
                    }
                } if self.device_token else {})
            },
            "playbackDataRequest": {},
            "timedTextUrlsRequest": {
                "supportedTimedTextFormats": ["TTMLv2", "DFXP"]
            },
            "trickplayUrlsRequest": {},
            "transitionTimecodesRequest": {},
            "vodPlaybackUrlsRequest":
                {
                    "device": {
                        "hdcpLevel": "2.2" if quality == "UHD" else "1.4",
                        "maxVideoResolution": (
                            "1080p" if quality == "HD" else
                            "480p" if quality == "SD" else
                            "2160p"
                        ),
                        "supportedStreamingTechnologies": ["DASH"],
                        "streamingTechnologies": {
                            "DASH": {
                                "bitrateAdaptations": ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode],
                                "codecs": [video_codec],
                                "drmKeyScheme": "SingleKey" if self.playready else "DualKey",
                                "drmType": "PlayReady" if self.playready else "Widevine",
                                "dynamicRangeFormats": self.VIDEO_RANGE_MAP.get(hdr, "None"),
                                "fragmentRepresentations": ["ByteOffsetRange", "SeparateFile"],
                                "frameRates": ["Standard"],
                                #"stitchType": "MultiPeriod",
                                "segmentInfoType": "Base",
                                "timedTextRepresentations": [
                                    "NotInManifestNorStream",
                                    "SeparateStreamInManifest"
                                ],
                                "trickplayRepresentations": ["NotInManifestNorStream"],
                                "variableAspectRatio": "supported"
                            }
                        },
                        "displayWidth": 4096,
                        "displayHeight": 2304
                    },
                    "ads": {
                        "sitePageUrl": "",
                        "gdpr": {
                            "enabled": "false",
                            "consentMap": {}
                        }
                    },
                    "playbackCustomizations": {},
                    "playbackSettingsRequest": {
                        "firmware": "UNKNOWN",
                        "playerType": self.player,
                        "responseFormatVersion": "1.0.0",
                        "titleId": title.id
                    }
                } if not self.device_token else {
                    "ads": {},
                    "device": {
                        "displayBasedVending": "supported",
                        "displayHeight": 2304,
                        "displayWidth": 4096,
                        "streamingTechnologies": {
                            "DASH": {
                                "fragmentRepresentations": [
                                    "ByteOffsetRange",
                                    "SeparateFile"
                                ],
                                "manifestThinningToSupportedResolution": "Forbidden",
                                "segmentInfoType": "List",
                                #"stitchType": "MultiPeriod",
                                "timedTextRepresentations": [
                                    "BurnedIn",
                                    "NotInManifestNorStream",
                                    "SeparateStreamInManifest"
                                ],
                                "trickplayRepresentations": [
                                    "NotInManifestNorStream"
                                ],
                                "variableAspectRatio": "supported",
                                "vastTimelineType": "Absolute",
                                "bitrateAdaptations": ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode],
                                "codecs": [video_codec],
                                "drmKeyScheme": "SingleKey",
                                "drmStrength": "L40",
                                "drmType": "PlayReady" if self.playready else "Widevine",
                                "dynamicRangeFormats": [self.VIDEO_RANGE_MAP.get(hdr, "None")],
                                "frameRates": ["Standard"]
                            },
                            "SmoothStreaming": {
                                "fragmentRepresentations": [
                                    "ByteOffsetRange",
                                    "SeparateFile"
                                ],
                                "manifestThinningToSupportedResolution": "Forbidden",
                                "segmentInfoType": "List",
                                #"stitchType": "MultiPeriod",
                                "timedTextRepresentations": [
                                    "BurnedIn",
                                    "NotInManifestNorStream",
                                    "SeparateStreamInManifest"
                                ],
                                "trickplayRepresentations": [
                                    "NotInManifestNorStream"
                                ],
                                "variableAspectRatio": "supported",
                                "vastTimelineType": "Absolute",
                                "bitrateAdaptations": ["CVBR", "CBR"] if bitrate_mode in ("CVBR+CBR", "CVBR,CBR") else [bitrate_mode],
                                "codecs": [video_codec],
                                "drmKeyScheme": "SingleKey",
                                "drmStrength": "L40",
                                "drmType": "PlayReady",
                                "dynamicRangeFormats": [self.VIDEO_RANGE_MAP.get(hdr, "None")],
                                "frameRates": ["Standard"]
                            }
                        },
                        "acceptedCreativeApis": [],
                        "category": "Tv",
                        "hdcpLevel": "2.2",
                        "maxVideoResolution": "2160p",
                        "platform": "Android",
                        "supportedStreamingTechnologies": [
                            "DASH", "SmoothStreaming"
                        ]
                    },
                    "playbackCustomizations": {},
                    "playbackSettingsRequest": {
                        "firmware": "UNKNOWN",
                        "playerType": self.player,
                        "responseFormatVersion": "1.0.0",
                        "titleId": title.id
                    }
                },
                "vodXrayMetadataRequest": {
                    "xrayDeviceClass": "normal",
                    "xrayPlaybackMode": "playback",
                    "xrayToken": "XRAY_WEB_2023_V2"
                }
            }

        json_data = json.dumps(data_dict)

        res = self.session.post(
            url=self.endpoints["playback"],
            params={
                "deviceID": self.device_id,
                "deviceTypeID": self.device["device_type"],
                "gascEnabled": str(self.pv).lower(),
                "marketplaceID": self.region["marketplace_id"],
                "uxLocale": "en_EN",
                "firmware": "1",
                "titleId": title.id,
                "nerid": self.generate_nerid(),
            },
            data=json_data,
            headers={
                "Authorization": f"Bearer {self.device_token}" if self.device_token else None,
            },
        )
        try:
            manifest = res.json()
        except json.JSONDecodeError:
            if ignore_errors:
                return {}

            raise self.log.exit(f" - Amazon reported an error when obtaining the Playback Manifest\n{res.text}")

        if "error" in manifest["vodPlaybackUrls"]:
            if ignore_errors:
                return {}
            message = manifest["vodPlaybackUrls"]["error"]["message"]
            raise self.log.exit(f" - Amazon reported an error when obtaining the Playback Manifest: {message}")

        # Commented out as we move the rights exception check elsewhere
        # if "rightsException" in manifest["returnedTitleRendition"]["selectedEntitlement"]:
        #     if ignore_errors:
        #         return {}
        #     raise self.log.exit(" - The profile used does not have the rights to this title.")

        # Below checks ignore NoRights errors

        if (
          manifest.get("errorsByResource", {}).get("PlaybackUrls") and
          manifest["errorsByResource"]["PlaybackUrls"].get("errorCode") != "PRS.NoRights.NotOwned"
        ):
            if ignore_errors:
                return {}
            error = manifest["errorsByResource"]["PlaybackUrls"]
            raise self.log.exit(f" - Amazon had an error with the Playback Urls: {error['message']} [{error['errorCode']}]")

        if (
          manifest.get("errorsByResource", {}).get("AudioVideoUrls") and
          manifest["errorsByResource"]["AudioVideoUrls"].get("errorCode") != "PRS.NoRights.NotOwned"
        ):
            if ignore_errors:
                return {}
            error = manifest["errorsByResource"]["AudioVideoUrls"]
            raise self.log.exit(f" - Amazon had an error with the A/V Urls: {error['message']} [{error['errorCode']}]")

        return manifest
    
    def get_episodes(self, titleID):
        titles = []
        titles_ = []
        res = self.session.get(
            url=self.endpoints["details"],
            params={
                "titleID": titleID,
                "isElcano": "1",
                "sections": ["Atf", "Btf"]
            },
            headers={
                "Accept": "application/json"
            }
        )

        if not res.ok:
            raise self.log.exit(f"Unable to get title: {res.text} [{res.status_code}]")

        data = res.json()["widgets"]
        seasons = [x.get("titleID") for x in data["seasonSelector"]]

        for season in seasons:
            res = self.session.get(
                url=self.endpoints["details"],
                params={"titleID": season, "isElcano": "1", "sections": "Btf"},
                headers={"Accept": "application/json"},
            ).json()["widgets"]

            try:
                episode_list_data = res.get("episodeList", {})
                episodes = episode_list_data.get("episodes", [])
            except:
                continue

            product_details = res["productDetails"]["detail"]
            season_number = product_details.get("seasonNumber", 1)

            # Process initial episodes
            episodes_titles = []
            for episode in episodes:
                details = episode["detail"]
                episodes_titles.append(details["catalogId"])
                titles.append(
                    Title(
                        id_=details["catalogId"],
                        type_=Title.Types.TV,
                        name=product_details["parentTitle"],
                        season=season_number,
                        episode=episode["self"]["sequenceNumber"],
                        episode_name=details["title"],
                        original_lang=None,
                        source=self.ALIASES[0],
                        service_data=episode,
                    )
                )

            # Get playback info for initial batch
            playbackEnvelope_info = self.playbackEnvelope_data(episodes_titles)
            for title in titles:
                for playbackInfo in playbackEnvelope_info:
                    if title.id == playbackInfo["titleID"]:
                        title.service_data.update({"playbackInfo": playbackInfo})
                        titles_.append(title)

            # Handle pagination if there are more episodes
            pagination_data = episode_list_data.get('actions', {}).get('pagination', [])
            token = next((item.get('token') for item in pagination_data if item.get('tokenType') == 'NextPage'), None)
            
            page_count = 1
            while token:
                page_count += 1
                self.log.info(f" + Loading page {page_count} for season {season_number}...")
                
                res = self.session.get(
                    url=self.endpoints["getDetailWidgets"],
                    params={
                        "titleID": season,
                        "isTvodOnRow": "1",
                        "widgets": f'[{{"widgetType":"EpisodeList","widgetToken":"{quote(token)}"}}]'
                    },
                    headers={
                        "Accept": "application/json"
                    }
                )
                
                if not res.ok:
                    self.log.warning(f"Failed to get page {page_count}: {res.status_code}")
                    break
                    
                page_data = res.json()
                episodeList = page_data.get('widgets', {}).get('episodeList', {})
                page_episodes = episodeList.get('episodes', [])
                
                if not page_episodes:
                    break
                
                episodes_titles = []
                for item in page_episodes:
                    details = item["detail"]
                    episode_num = int(item.get('self', {}).get('sequenceNumber', 0))
                    episodes_titles.append(details["catalogId"])
                    titles.append(Title(
                        id_=details["catalogId"],
                        type_=Title.Types.TV,
                        name=product_details["parentTitle"],
                        season=season_number,
                        episode=episode_num,
                        episode_name=details["title"],
                        original_lang=None,
                        source=self.ALIASES[0],
                        service_data=item
                    ))
                
                # Get playback info for this batch
                playbackEnvelope_info = self.playbackEnvelope_data(episodes_titles)
                for title in titles:
                    for playbackInfo in playbackEnvelope_info:
                        if title.id == playbackInfo["titleID"]:
                            title.service_data.update({"playbackInfo": playbackInfo})
                            if title not in titles_:
                                titles_.append(title)
                
                # Get next page token
                pagination_data = episodeList.get('actions', {}).get('pagination', [])
                token = next((item.get('token') for item in pagination_data if item.get('tokenType') == 'NextPage'), None)
                
        return titles_

    @staticmethod
    def get_original_language(manifest):
        """Get a title's original language from manifest data."""
        try:
            return next(
                x["language"].replace("_", "-")
                for x in manifest["catalogMetadata"]["playback"]["audioTracks"]
                if x["isOriginalLanguage"]
            )
        except (KeyError, StopIteration):
            pass

        if "defaultAudioTrackId" in manifest.get("playbackUrls", {}):
            try:
                return manifest["playbackUrls"]["defaultAudioTrackId"].split("_")[0]
            except IndexError:
                pass

        try:
            return sorted(
                manifest["audioVideoUrls"]["audioTrackMetadata"],
                key=lambda x: x["index"]
            )[0]["languageCode"]
        except (KeyError, IndexError):
            pass

        return None
    
    @staticmethod
    def generate_nerid(e=0):
        """Generates Network Edge Request ID."""
        BASE64_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    
        # Timestamp part (7 chars)
        timestamp = int(time.time() * 1000)
        ts_chars = []
        for _ in range(7):
            ts_chars.append(BASE64_CHARS[timestamp % 64])
            timestamp //= 64
            ts_part = ''.join(reversed(ts_chars))
    
        # Random part (15 chars)
        rand_part = ''.join(secrets.choice(BASE64_CHARS) for _ in range(15))
    
        # Suffix (2 digits, zero-padded)
        suffix = f"{e % 100:02d}"
    
        return ts_part + rand_part + suffix


    @staticmethod
    def clean_mpd_url(mpd_url, optimise=False):
        """Clean up an Amazon MPD manifest url."""
        if '@' in mpd_url:
            mpd_url = re.sub(r'/\d+@[^/]+', '', mpd_url, count=1)
        if optimise:
            return mpd_url.replace("~", "") + "?encoding=segmentBase"
        if match := re.match(r"(https?://.*/)d.?/.*~/(.*)", mpd_url):
            mpd_url = "".join(match.groups())
        else:
            try:
                mpd_url = "".join(
                    re.split(r"(?i)(/)", mpd_url)[:5] + re.split(r"(?i)(/)", mpd_url)[9:]
                )
            except IndexError:
                raise IndexError("Unable to parse MPD URL")

        return mpd_url
        
        
    def get_best_quality(self, title):
        """
        Choose the best quality manifest from CBR / CVBR
        """

        tracks = Tracks()
        bitrates = [self.orig_bitrate]

        if self.vcodec != "H265":
            bitrates = self.orig_bitrate.split('+')

        for bitrate in bitrates:
            manifest = self.get_manifest(
                title,
                video_codec=self.vcodec,
                bitrate_mode=bitrate,
                quality=self.vquality,
                hdr=self.range,
                ignore_errors=False
            )

            if not manifest:
                self.log.warning(f"Skipping {bitrate} manifest due to error")
                continue

            bitrate = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["bitrateAdaptation"]
                
            # return three empty objects if a rightsException error exists to correlate to manifest, chosen_manifest, tracks
            #if "rightsException" in manifest["returnedTitleRendition"]["selectedEntitlement"]:
                #return None, None, None

            #self.customer_id = manifest["returnedTitleRendition"]["selectedEntitlement"]["grantedByCustomerId"]

            #default_url_set = manifest["playbackUrls"]["urlSets"][manifest["playbackUrls"]["defaultUrlSetId"]]
            #encoding_version = default_url_set["urls"]["manifest"]["encodingVersion"]
            #self.log.info(f" + Detected encodingVersion={encoding_version}")

            chosen_manifest = self.choose_manifest(manifest, self.cdn)

            if not chosen_manifest:
                self.log.warning(f"No {bitrate} DASH manifests available")
                continue

            mpd_url = self.clean_mpd_url(chosen_manifest["url"], optimise=False)
            self.log.debug(mpd_url)
            if self.event:
                devicetype = self.device["device_type"]
                mpd_url = chosen_manifest["url"]
                mpd_url = f"{mpd_url}?amznDtid={devicetype}&encoding=segmentBase"
            self.log.info(f" + Downloading {bitrate} MPD")

            streamingProtocol = manifest["vodPlaybackUrls"]["result"]["playbackUrls"]["urlMetadata"]["streamingProtocol"]
            sessionHandoffToken = manifest["sessionization"]["sessionHandoffToken"]

            if streamingProtocol == "DASH":
                tracks.add(Tracks.from_mpd(
                        url=mpd_url,
                        session=self.session,
                        source=self.ALIASES[0],
                ))
                for track in tracks:
                    track.extra = track.extra + (sessionHandoffToken,)
            elif streamingProtocol == "SmoothStreaming":
                tracks.add(Tracks.from_ism(
                        url=mpd_url,
                        source=self.ALIASES[0],
                    ))
                for track in tracks:
                    track.extra = track.extra + (sessionHandoffToken,)
            else:
                raise self.log.exit(f"Unsupported manifest type: {streamingProtocol}")

            for video in tracks.videos:
                video.note = bitrate
            
        if len(self.bitrate.split('+')) > 1:
            self.bitrate = "CVBR,CBR"
            self.log.info("Selected video manifest bitrate: %s", self.bitrate)

        return tracks

    # Service specific classes

    class DeviceRegistration:

        def __init__(self, device: dict, endpoints: dict, cache_path: Path, session: requests.Session, log: Logger):
            self.session = session
            self.device = device
            self.endpoints = endpoints
            self.cache_path = cache_path
            self.log = log

            self.device = {k: str(v) if not isinstance(v, str) else v for k, v in self.device.items()}

            self.bearer = None
            if os.path.isfile(self.cache_path):
                with open(self.cache_path, encoding="utf-8") as fd:
                    cache = jsonpickle.decode(fd.read())
                #self.device["device_serial"] = cache["device_serial"]
                if cache.get("expires_in", 0) > int(time.time()):
                    # not expired, lets use
                    self.log.info(" + Using cached device bearer")
                    self.bearer = cache["access_token"]
                else:
                    # expired, refresh
                    self.log.info("Cached device bearer expired, refreshing...")
                    refreshed_tokens = self.refresh(self.device, cache["refresh_token"])
                    refreshed_tokens["refresh_token"] = cache["refresh_token"]
                    # expires_in seems to be in minutes, create a unix timestamp and add the minutes in seconds
                    refreshed_tokens["expires_in"] = int(time.time()) + int(refreshed_tokens["expires_in"])
                    with open(self.cache_path, "w", encoding="utf-8") as fd:
                        fd.write(jsonpickle.encode(refreshed_tokens))
                    self.bearer = refreshed_tokens["access_token"]
            else:
                self.log.info(" + Registering new device bearer")
                self.bearer = self.register(self.device)

        def register(self, device: dict) -> dict:
            """
            Register device to the account
            :param device: Device data to register
            :return: Device bearer tokens
            """
            # OnTV csrf
            csrf_token = self.get_csrf_token()

            # Code pair
            code_pair = self.get_code_pair(device)

            # Device link
            response = self.session.post(
                url=self.endpoints["devicelink"],
                headers={
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9,es-US;q=0.8,es;q=0.7",  # needed?
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": self.endpoints["ontv"]
                },
                params=urlencode({
                    # any reason it urlencodes here? requests can take a param dict...
                    "ref_": "atv_set_rd_reg",
                    "publicCode": code_pair["public_code"],  # public code pair
                    "token": csrf_token  # csrf token
                })
            )
            if response.status_code != 200:
                raise self.log.exit(f"Unexpected response with the codeBasedLinking request: {response.text} [{response.status_code}]")

            # Register
            response = self.session.post(
                url=self.endpoints["register"],
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US"
                },
                json={
                    "auth_data": {
                        "code_pair": code_pair
                    },
                    "registration_data": device,
                    "requested_token_type": ["bearer"],
                    "requested_extensions": ["device_info", "customer_info"]
                },
                cookies=None  # for some reason, may fail if cookies are present. Odd.
            )
            if response.status_code != 200:
                raise self.log.exit(f"Unable to register: {response.text} [{response.status_code}]")
            bearer = response.json()["response"]["success"]["tokens"]["bearer"]
            bearer["expires_in"] = int(time.time()) + int(bearer["expires_in"])

            # Cache bearer
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with open(self.cache_path, "w", encoding="utf-8") as fd:
                fd.write(jsonpickle.encode(bearer))

            return bearer["access_token"]

        def refresh(self, device: dict, refresh_token: str) -> dict:
            response = self.session.post(
                url=self.endpoints["token"],
                json={
                    "app_name": device["app_name"],
                    "app_version": device["app_version"],
                    "source_token_type": "refresh_token",
                    "source_token": refresh_token,
                    "requested_token_type": "access_token"
                }
            ).json()
            if "error" in response:
                Path(self.cache_path).unlink(missing_ok=True)  # Remove the cached device as its tokens have expired
                raise self.log.exit(
                    f"Failed to refresh device token: {response['error_description']} [{response['error']}]"
                )
            if response["token_type"] != "bearer":
                raise self.log.exit("Unexpected returned refreshed token type")
            return response

        def get_csrf_token(self) -> str:
            """
            On the amazon website, you need a token that is in the html page,
            this token is used to register the device
            :return: OnTV Page's CSRF Token
            """
            res = self.session.get(self.endpoints["ontv"])
            response = res.text
            if 'input type="hidden" name="appAction" value="SIGNIN"' in response:
                raise self.log.exit(
                    "Cookies are signed out, cannot get ontv CSRF token. "
                    f"Expecting profile to have cookies for: {self.endpoints['ontv']}"
                )
            for match in re.finditer(r"<script type=\"text/template\">(.+)</script>", response):
                prop = json.loads(match.group(1))
                prop = prop.get("props", {}).get("codeEntry", {}).get("token")
                if prop:
                    return prop
            raise self.log.exit("Unable to get ontv CSRF token")

        def get_code_pair(self, device: dict) -> dict:
            """
            Getting code pairs based on the device that you are using
            :return: public and private code pairs
            """
            res = self.session.post(
                url=self.endpoints["codepair"],
                headers={
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US"
                },
                json={"code_data": device}
            ).json()
            if "error" in res:
                raise self.log.exit(f"Unable to get code pair: {res['error_description']} [{res['error']}]")
            return res
