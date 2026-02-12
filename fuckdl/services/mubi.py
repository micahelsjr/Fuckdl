import base64
import json
import re
import click
import os, sys

import uuid
import xmltodict

from langcodes import Language
from fuckdl.objects import AudioTrack, TextTrack, Title, Tracks, VideoTrack
from fuckdl.services.BaseService import BaseService
from fuckdl.vendor.pymp4.parser import Box


class Mubi(BaseService):
    """
    Made By redd / Edit by superman
    and Widevine Group - Chrome CDM API dont share this


    \b
    Authorization: Credentials 
    Security: UHD@L3, doesn't care about releases.
    """

    ALIASES = ["MUBI"]

    TITLE_RE = [
        r'/(?P<id>[^/]+)$',
        r"^(?:https?://(?:www\.)?mubi\.com\/)?(?P<id>[^/]+)$",
    ]

    AUDIO_CODEC_MAP = {
        "AAC": "mp4a",
        "AC3": "ac-3",
        "EC3": "ec-3"
    }

    @staticmethod
    @click.command(name="Mubi", short_help="https://mubi.com/")
    @click.argument("title", type=str, required=False)
   
    @click.pass_context
    def cli(ctx, **kwargs):
        return Mubi(ctx, **kwargs)

    def __init__(self, ctx, title):
        super().__init__(ctx)
        self.parse_title(ctx, title)

        self.vcodec = ctx.parent.params["vcodec"].lower()
        self.acodec = ctx.parent.params["acodec"]
        self.range = ctx.parent.params["range_"]
        self.bearer= None
        self.dtinfo= None
        self.quality = ctx.parent.params["quality"]
        self.headers = {
                "authority": "api.mubi.com",
                "accept": "application/json",
                "accept-language": "en-US",
                "client": self.config["device"]["client_name"],
                "client-version": "20.2",
                "client-device-identifier": self.config["device"]["device_identifier"],
                "client-app": "mubi",
                "client-device-brand": "Google",
                'client-accept-audio-codecs': 'eac3, ac3, aac',
                "client-device-model": self.config["device"]["device_model"],
                "client-device-os": self.config["device"]["device_os"],
                "client-country": "US",
                "content-type": "application/json; charset=UTF-8",
                "host": "api.mubi.com",
                "connection": "Keep-Alive",
                "accept-encoding": "gzip",
                "user-agent": self.config["device"]["user_agent"],
            }
        if self.vcodec=="vp9":
            self.headers["client-accept-video-codecs"]="vp9"
        elif self.vcodec=="h265":
            self.headers["client-accept-video-codecs"]="h265"
        elif self.vcodec=="h264":
            self.headers["client-accept-video-codecs"]="h264"
        else:
            self.headers["client-accept-video-codecs"]="vp9,h265,h264"

        self.configure()

    def get_titles(self):
        self.log.info(" + Getting Metadata.")
        res = self.session.get(
                self.config["endpoints"]["metadata"].format(title_id=self.title)
                ,headers=self.headers).json()
        try:
            res
        except json.JSONDecodeError:
            raise self.log.exit(f" - Failed to load title metadata: {res.text}")
        
        self.title = res['id']   
        
        return Title(
                id_=self.title,
                type_=Title.Types.MOVIE,
                name=res["title"],
                year=res["year"],
                source=self.ALIASES[0],
                service_data=res,
            )
    
    def create_pssh_from_kid(self, kid: str):
        WV_SYSTEM_ID = [237, 239, 139, 169, 121, 214, 74, 206, 163, 200, 39, 220, 213, 29, 33, 237]
        kid = uuid.UUID(kid).bytes

        init_data = bytearray(b'\x12\x10')
        init_data.extend(kid)
        init_data.extend(b'H\xe3\xdc\x95\x9b\x06')

        pssh = bytearray([0, 0, 0])
        pssh.append(32 + len(init_data))
        pssh[4:] = bytearray(b'pssh')
        pssh[8:] = [0, 0, 0, 0]
        pssh[13:] = WV_SYSTEM_ID
        pssh[29:] = [0, 0, 0, 0]
        pssh[31] = len(init_data)
        pssh[32:] = init_data

        return base64.b64encode(pssh).decode('UTF-8')
    
    def get_pssh_from_mpd(self, mpd_url):
        r = self.session.get(mpd_url, headers=self.headers)

        if r.status_code != 200:
            raise Exception(r.text)

        mpd = xmltodict.parse(r.text, dict_constructor=dict)

        for adaption in mpd['MPD']['Period']['AdaptationSet']:
            if adaption['@mimeType'] == 'video/mp4':
                if 'ContentProtection' in adaption:
                    for protection in adaption['ContentProtection']:
                        if protection['@schemeIdUri'].lower() == 'urn:mpeg:dash:mp4protection:2011':
                            return self.create_pssh_from_kid(protection['@cenc:default_KID'])

    def get_tracks(self, title):
        res = self.session.post(
            self.config["endpoints"]["viewing"].format(title_id=self.title),headers=self.headers
        ).json()

        lang = res["audio_track_id"]
        title.original_lang = lang.replace('audio_main_', '')
        
        data = self.session.get(
            self.config["endpoints"]["manifest"].format(title_id=self.title),headers=self.headers
        ).json()

        if self.quality==2160:
            mpd_url = re.sub(r"/default/.*\.mpd$", "/default/2160.mpd", data["url"])
        else:
            mpd_url=data["url"]
        
        pssh = self.get_pssh_from_mpd(mpd_url)
        video_pssh = Box.parse(base64.b64decode(pssh))

        tracks=Tracks.from_mpd(
            url=mpd_url,
            session=self.session,
            source=self.ALIASES[0],
        )

# FIX: Checks whether the pssh attribute exists before accessing it
# And defines both pssh and psshWV for compatibility with dl.py
        for track in tracks.videos:
            if not hasattr(track, 'pssh') or not track.pssh:            
                track.pssh = video_pssh
                track.psshWV = pssh  # Adds psshWV in string format
        
        for track in tracks.audios:
            if not hasattr(track, 'pssh') or not track.pssh:
                track.pssh = video_pssh
                track.psshWV = pssh  # Adds psshWV in string format

        if self.acodec:
            tracks.audios = [x for x in tracks.audios if (x.codec or "")[:4] == self.AUDIO_CODEC_MAP[self.acodec]]

        return tracks

    def get_chapters(self, title):
        return []

    def certificate(self, **kwargs):
        print("DEBUG: certificate() method CALLED!")
        self.log.info(" + Certificate method called")
        return None

    def license(self, challenge, **kwargs):
        """
        Método de licença com debug completo
        """
        print("\n" + "="*80)
        print("DEBUG: license() method CALLED!")
        print(f"Challenge type: {type(challenge)}, length: {len(challenge) if challenge else 0}")
        print(f"dt-custom-data exists: {self.dtinfo is not None}")
        print(f"License URL: {self.config['endpoints']['license']}")
        print("="*80 + "\n")
        
        self.log.info(" + Requesting License...")
        self.log.debug(f" + License URL: {self.config['endpoints']['license']}")
        self.log.debug(f" + dt-custom-data: {self.dtinfo[:100] if self.dtinfo else 'None'}...")
        self.log.debug(f" + Challenge length: {len(challenge)} bytes")
        
        headers = {
            "dt-custom-data": self.dtinfo,
            "Content-Type": "application/octet-stream",
            "User-Agent": self.config["device"]["user_agent"],
            "Accept": "*/*"
        }
        
        try:
            self.log.debug(" + Sending POST request to license server...")
            
            response = self.session.post(
                url=self.config["endpoints"]["license"],
                headers=headers,
                data=challenge,
                timeout=30
            )
            
            self.log.info(f" + License Response Status: {response.status_code}")
            self.log.debug(f" + Response Headers: {dict(response.headers)}")
            
            if response.status_code != 200:
                self.log.error(f" - License request failed!")
                self.log.error(f" - Status Code: {response.status_code}")
                self.log.error(f" - Response Text: {response.text[:1000]}")
                return None
            
            if not response.content:
                self.log.error(" - License response is empty!")
                return None
            
            # DRM Today returns JSON, we need to extract the license
            try:
                license_data = response.json()
                self.log.debug(f" + License JSON keys: {license_data.keys()}")
                
                # The license may be in 'license' or 'payload'
                if 'license' in license_data:
                    license_b64 = license_data['license']
                elif 'payload' in license_data:
                    license_b64 = license_data['payload']
                else:
                    self.log.error(f" - Unexpected JSON structure: {list(license_data.keys())}")
                    return None
                
                # Decodes from base64 to bytes
                license_bytes = base64.b64decode(license_b64)
                self.log.info(f" + License received: {len(license_bytes)} bytes (decoded from base64)")
                return license_bytes
                
            except json.JSONDecodeError:
                # If it's not JSON, return it as-is (binary)
                self.log.info(f" + License received: {len(response.content)} bytes (binary)")
                return response.content
            
        except Exception as e:
            self.log.error(f" - Exception during license request: {type(e).__name__}")
            self.log.error(f" - Error message: {str(e)}")
            import traceback
            self.log.debug(f" - Full traceback:\n{traceback.format_exc()}")
            return None
        
    def configure(self):
        tokens_cache_path = self.get_cache("tokens_mubi.json")
        self.log.info(" + Loading Cached Token...")
        
        if os.path.isfile(tokens_cache_path):
            with open(tokens_cache_path, encoding="utf-8") as fd:
                tokens = json.load(fd)
            self.bearer = tokens["authorization"]
            self.dtinfo = tokens["dt-custom-data"]
            self.headers["authorization"] = self.bearer
            
            self.log.debug(f" + Loaded bearer token: {self.bearer[:50]}...")
            self.log.debug(f" + Loaded dt-custom-data: {self.dtinfo[:50]}...")
        else:
            self.log.info(" + Retrieving API configuration")
            if not self.credentials.username:
                raise self.log.exit(" - No cookies provided, cannot log in.")
            
            req_payload = json.dumps({
                "identifier": self.credentials.username,
                "magic_link": True
            })
            
            auth_resp = self.session.post(
                url=self.config["endpoints"]["authtok_url"], 
                data=req_payload, 
                headers=self.headers
            ).json()
            
            payload = json.dumps({
                "auth_request_token": auth_resp["auth_request_token"],
                "identifier": self.credentials.username,
                "password": self.credentials.password
            })
            
            response = self.session.post(
                url=self.config["endpoints"]["loginurl"], 
                data=payload, 
                headers=self.headers
            ).json()
            
            json_str = {
                "merchant": "mubi",
                "sessionId": response["token"],
                "userId": str(response["user"]["id"])
            }
            
            self.bearer = "Bearer " + response["token"]
            # Uses json.dumps to ensure proper formatting
            self.dtinfo = base64.b64encode(json.dumps(json_str).encode('utf-8')).decode('utf-8')
            
            save_data = {
                "authorization": self.bearer,
                "dt-custom-data": self.dtinfo
            }

            os.makedirs(os.path.dirname(tokens_cache_path), exist_ok=True)
            with open(tokens_cache_path, "w", encoding="utf-8") as fd:
                json.dump(save_data, fd)
            
            self.log.info(f" + Token saved to cache")
            self.log.debug(f" + Bearer: {self.bearer[:50]}...")
            self.log.debug(f" + dt-custom-data: {self.dtinfo[:50]}...")