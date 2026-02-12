import logging
import os
import sys
from datetime import datetime

import click
import coloredlogs

from fuckdl.config import directories, filenames  # isort: split
from fuckdl.commands import dl

# Try to import colorama for colored ASCII art
try:
    from colorama import Fore, Style, init
    init(autoreset=True)
    COLORAMA_AVAILABLE = True
except ImportError:
    COLORAMA_AVAILABLE = False


def get_ascii_art():
    """Generate ASCII art banner with red FuckDL text."""
    # Bigger ASCII art for FuckDL
    fuckdl_text = """
  ______          _      _____  _      
 |  ____|        | |    |  __ \| |     
 | |__ _   _  ___| | __ | |  | | |     
 |  __| | | |/ __| |/ / | |  | | |     
 | |  | |_| | (__|   <  | |__| | |____ 
 |_|   \__,_|\___|_|\_\ |_____/|______|
                                       
"""
    
    banner = """
================================================================================
                                                                              
"""
    
    # Add red colored FuckDL text if colorama is available
    if COLORAMA_AVAILABLE:
        banner += Fore.RED + fuckdl_text + Style.RESET_ALL
    else:
        banner += fuckdl_text
    
    banner += """
    Playready and Widevine DRM downloader and decrypter                       
                                                                              
    +------------------------------------------------------------------+      
    |                    Created By Barbie DRM                         |      
    |                  https://t.me/barbiedrm                          |      
    +------------------------------------------------------------------+      
                                                                              
================================================================================
"""
    return banner


@click.command(context_settings=dict(
    allow_extra_args=True,
    ignore_unknown_options=True,
    max_content_width=116,  # max PEP8 line-width, -4 to adjust for initial indent
))
@click.option("--debug", is_flag=True, default=False,
              help="Enable DEBUG level logs on the console. This is always enabled for log files.")
def main(debug):
    """
    fuckdl is the most convenient command-line program to
    download videos from Playready and Widevine DRM-protected video platforms.
    """
    LOG_FORMAT = "{asctime} [{levelname[0]}] {name} : {message}"
    LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
    LOG_STYLE = "{"

    def log_exit(self, msg, *args, **kwargs):
        self.critical(msg, *args, **kwargs)
        sys.exit(1)

    logging.Logger.exit = log_exit

    os.makedirs(directories.logs, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        style=LOG_STYLE,
        handlers=[logging.FileHandler(
            os.path.join(directories.logs, filenames.log.format(time=datetime.now().strftime("%Y%m%d-%H%M%S"))),
            encoding='utf-8'
        )]
    )

    coloredlogs.install(
        level=logging.DEBUG if debug else logging.INFO,
        fmt=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
        style=LOG_STYLE,
        handlers=[logging.StreamHandler()],
    )

    # Display ASCII art banner
    print(get_ascii_art())

    log = logging.getLogger("vt")

    log.info("fuckdl - Playready and Widevine DRM downloader and decrypter")
    log.info(f"[Root Config]     : {filenames.user_root_config}")
    log.info(f"[Service Configs] : {directories.service_configs}")
    log.info(f"[Cookies]         : {directories.cookies}")
    log.info(f"[CDM Devices]     : {directories.devices}")
    log.info(f"[Cache]           : {directories.cache}")
    log.info(f"[Logs]            : {directories.logs}")
    log.info(f"[Temp Files]      : {directories.temp}")
    log.info(f"[Downloads]       : {directories.downloads}")
    
    os.environ['PATH'] = os.path.abspath('./binaries')

    if len(sys.argv) > 1 and sys.argv[1].lower() == "dl":
        sys.argv.pop(1)

    dl()


if __name__ == "__main__":
    main()
