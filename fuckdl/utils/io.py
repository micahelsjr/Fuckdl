import asyncio
import contextlib
import os
import re
import shutil
import subprocess
import sys
import httpx
import pproxy
import requests
import yaml
import tqdm
import logging
import time
from fuckdl import config
from fuckdl.utils.collections import as_list
from pathlib import Path
from typing import Union  # <-- AGREGAR ESTA IMPORTACIÓN


def load_yaml(path):
    if not os.path.isfile(path):
        return {}
    with open(path) as fd:
        return yaml.safe_load(fd)

def save_yaml(data: dict, path: Union[str, Path]) -> None:
    """
    Save data to a YAML file.
    
    Args:
        data: Data to save
        path: Path to save to
    """
    import yaml
    
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

_ip_info = None


def get_ip_info(session=None, fresh=False):
    """Use extreme-ip-lookup.com to get IP location information."""
    global _ip_info

    if fresh or not _ip_info:
        # alternatives: http://www.geoplugin.net/json.gp, http://ip-api.com/json/, https://extreme-ip-lookup.com/json
        _ip_info = (session or httpx).get("https://ipwho.is/").json()

    return _ip_info


@contextlib.asynccontextmanager
async def start_pproxy(host, port, username, password):
    rerouted_proxy = "http://localhost:8081"
    server = pproxy.Server(rerouted_proxy)
    remote = pproxy.Connection(f"http+ssl://{host}:{port}#{username}:{password}")
    handler = await server.start_server(dict(rserver=[remote]))
    try:
        yield rerouted_proxy
    finally:
        handler.close()
        await handler.wait_closed()



def download_range(url, count, start=0, proxy=None):
    """Download n bytes without using the Range header due to support issues."""
    # TODO: Can this be done with Aria2c?
    executable = shutil.which("curl")
    if not executable:
        raise EnvironmentError("Track needs curl to download a chunk of data but wasn't found...")

    arguments = [
        executable,
        "-s",  # use -s instead of --no-progress-meter due to version requirements
        "-L",  # follow redirects, e.g. http->https
        "--proxy-insecure",  # disable SSL verification of proxy
        "--output", "-",  # output to stdout
        "--url", url
    ]
    if proxy:
        arguments.extend(["--proxy", proxy])

    curl = subprocess.Popen(
        arguments,
        stdout=subprocess.PIPE,
        stderr=open(os.devnull, "wb"),
        shell=False
    )
    buffer = b''
    location = -1
    while len(buffer) < count:
        stdout = curl.stdout
        data = b''
        if stdout:
            data = stdout.read(1)
        if len(data) > 0:
            location += len(data)
            if location >= start:
                buffer += data
        else:
            if curl.poll() is not None:
                break
    curl.kill()  # stop downloading
    return buffer


# HBO Max specific constants
HBOMAX_ALIASES = ["HBOMAX", "hbomax", "HBO Max", "hbo max", "MAX", "max"]
HBOMAX_URL_PATTERNS = [
    "dfw-nbl.latam.prd.media.max.com",
    "hbomax.com", 
    "max.com",
    "hbo.com",
    ".max.com/gcs/",
    "media.max.com"
]

# HBO Max predefined headers (from working command)
HBOMAX_PREDEFINED_HEADERS = {
    "User-Agent": "BEAM-Android/5.0.0 (motorola/moto g(6) play)",
    "Accept": "application/json, text/plain, */*",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "x-disco-client": "ANDROID:9:beam:5.0.0",
    "x-disco-params": "realm=bolt,bid=beam,features=ar,rr",
    "x-device-info": "BEAM-Android/5.0.0 (motorola/moto g(6) play; ANDROID/9; 9cac27069847250f/b6746ddc-7bc7-471f-a16c-f6aaf0c34d26)",
    "Origin": "https://play.hbomax.com",
    "Referer": "https://play.hbomax.com/"
}

def is_hbomax_url(url):
    """Check if URL belongs to HBO Max service."""
    url_str = str(url).lower()
    return any(pattern.lower() in url_str for pattern in HBOMAX_URL_PATTERNS)


def is_hbomax_alias(service_name):
    """Check if service name is an HBO Max alias."""
    return any(alias.lower() == service_name.lower() for alias in HBOMAX_ALIASES)


def are_hbomax_headers_expired(headers):
    """Check if HBO Max headers have expired tokens."""
    if not headers:
        return False
    
    headers_str = str(headers)
    
    # Check for tracestate with expires field
    if "tracestate" in headers_str and "expires" in headers_str:
        import re
        match = re.search(r"'expires':\s*(\d+)", headers_str)
        if match:
            expires_timestamp = int(match.group(1)) / 1000  # Convert to seconds
            current_time = time.time()
            return current_time > expires_timestamp
    
    return False


def get_hbomax_predefined_headers():
    """Get predefined headers for HBO Max with updated expiration."""
    headers = HBOMAX_PREDEFINED_HEADERS.copy()
    
    # Update expiration timestamp (24 hours from now)
    current_time_ms = int(time.time() * 1000)
    expires_time_ms = current_time_ms + (24 * 60 * 60 * 1000)  # 24 hours
    
    # Update tracestate with new expiration
    old_tracestate = headers.get("tracestate", "")
    if "'expires':" in old_tracestate:
        # Replace the expiration timestamp
        import re
        new_tracestate = re.sub(
            r"'expires':\s*\d+",
            f"'expires': {expires_time_ms}",
            old_tracestate
        )
        headers["tracestate"] = new_tracestate
    
    return headers

async def aria2c_hbomax_specific(uri, out, proxy=None):
    """Specialized aria2c download for HBO Max with fresh headers."""
    executable = "C:\\DRMLab\\binaries\\aria2c.EXE"
    if not os.path.isfile(executable):
        executable = shutil.which("aria2c") or shutil.which("aria2")
        if not executable:
            raise EnvironmentError("Aria2c executable not found...")

    arguments = [
        executable,
        "-c", "--remote-time",
        "-o", os.path.basename(out),
        "-x", "8", "-j", "8", "-s", "8",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--retry-wait", "3",
        "--max-tries", "10",
        "--max-file-not-found", "5",
        "--summary-interval", "1",  # Mostrar progreso cada 1 segundo
        "--file-allocation", "none" if sys.platform == "win32" else "falloc",
        "--console-log-level", "info",  # Cambiado a "info" para ver progreso
        "--download-result", "default",
        "--file-allocation=prealloc",
        "--human-readable=true",
        "--quiet=false",
        "--show-console-readout=true",
        "--check-certificate=false",
        "--timeout=30",
        "--connect-timeout=30"
    ]

    # Obtener headers frescos con trace dinámicos
    hbomax_headers = get_hbomax_predefined_headers()
    
    for header, value in hbomax_headers.items():
        arguments.extend(["--header", f"{header}: {value}"])

    if proxy:
        arguments.extend(["--all-proxy", proxy])

    arguments.extend(["-d", os.path.dirname(out), uri])

    try:
        print(f"HBO Max - Downloading: {os.path.basename(out)}")
        
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Combinar stdout y stderr
            universal_newlines=True,
            bufsize=1,
            encoding='utf-8',
            errors='ignore',
            text=True
        )
        
        last_percentage = 0
        file_size_mb = 0
        download_speed = ""
        eta = ""
        
        # Procesar salida en tiempo real
        while True:
            line = process.stdout.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
            
            # Filtrar y mostrar solo líneas de progreso
            line = line.strip()
            
            # Detectar líneas de progreso (varios formatos posibles)
            if any(x in line for x in ["%", "CN:", "DL:", "ETA:", "Downloading"]):
                # Extraer información relevante
                import re
                
                # Buscar porcentaje: (XX%)
                percent_match = re.search(r'\((\d+)%\)', line)
                if percent_match:
                    current_percentage = int(percent_match.group(1))
                    if current_percentage != last_percentage:
                        last_percentage = current_percentage
                
                # Buscar tamaño del archivo
                size_match = re.search(r'(\d+\.?\d*[KMGT]?i?B)\/(\d+\.?\d*[KMGT]?i?B)', line)
                if size_match:
                    downloaded = size_match.group(1)
                    total = size_match.group(2)
                
                # Buscar velocidad
                speed_match = re.search(r'DL:(\s*\d+\.?\d*[KMGT]?i?B/s)', line)
                if speed_match:
                    download_speed = speed_match.group(1).strip()
                
                # Buscar ETA
                eta_match = re.search(r'ETA:(\s*[\dhms]+)', line)
                if eta_match:
                    eta = eta_match.group(1).strip()
                
                # Construir línea de progreso bonita
                progress_line = ""
                if 'total' in locals():
                    progress_line += f"{downloaded}/{total} "
                
                progress_line += f"({last_percentage}%) "
                
                if download_speed:
                    progress_line += f"| Speed: {download_speed} "
                
                if eta:
                    progress_line += f"| ETA: {eta}"
                
                # Mostrar progreso (sobrescribir la misma línea)
                if progress_line:
                    print(f"\r{progress_line}", end="", flush=True)
            
            # Mostrar errores importantes
            elif any(x in line.lower() for x in ["error", "failed", "exception"]):
                print(f"\n⚠️  {line}")
        
        # Limpiar línea y mostrar resultado final
        print()  # Nueva línea
        
        # Verificar resultado
        if process.returncode == 0:
            if os.path.exists(out) and os.path.getsize(out) > 1024:
                print(f"✓ Download completed: {os.path.basename(out)}")
                return True
            else:
                print(f"⚠️  Download completed but file may be empty")
                return False
        else:
            print(f"\n✗ Download failed (exit code: {process.returncode})")
            return False
        
    except Exception as e:
        print(f"\n✗ Error: {str(e)[:100]}")
        return False

async def aria2c(uri, out, headers=None, proxy=None):
    """
    Downloads file(s) using Aria2(c).

    Parameters:
        uri: URL to download. If uri is a list of urls, they will be downloaded and
          concatenated into one file.
        out: The output file path to save to.
        headers: Headers to apply on aria2c.
        proxy: Proxy to apply on aria2c.
    """
    # Check if this is HBO Max
    if is_hbomax_url(uri):
        # Check if headers are provided and if they're expired
        if not headers or are_hbomax_headers_expired(headers):
            print("HBO Max detected with no valid headers, using predefined headers...")
            return await aria2c_hbomax_specific(uri, out, proxy)
        else:
            print("HBO Max detected with valid headers, proceeding with provided headers...")

    # Continue with normal aria2c for non-HBO Max or HBO Max with valid headers
    executable = shutil.which("aria2c") or shutil.which("aria2")
    if not executable:
        raise EnvironmentError("Aria2c executable not found...")

    arguments = [
        executable,
        "-c",  # Continue downloading a partially downloaded file
        "--remote-time",  # Retrieve timestamp of the remote file from the and apply if available
        "-o", os.path.basename(out),  # The file name of the downloaded file, relative to -d
        "-x", "16",  # The maximum number of connections to one server for each download
        "-j", "16",  # The maximum number of parallel downloads for every static (HTTP/FTP) URL
        "-s", "16",  # Download a file using N connections.
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--retry-wait", "5",  # Set the seconds to wait between retries.
        "--max-tries", "15",
        "--max-file-not-found", "15",
        "--summary-interval", "0",
        "--file-allocation", "none" if sys.platform == "win32" else "falloc",
        "--console-log-level", "warn",
        "--download-result", "hide"
    ]

    for option, value in config.config.aria2c.items():
        arguments.append(f"--{option.replace('_', '-')}={value}")

    for header, value in (headers or {}).items():
        if header.lower() == "accept-encoding":
            # we cannot set an allowed encoding, or it will return compressed
            # and the code is not set up to uncompress the data
            continue
        arguments.extend(["--header", f"{header}: {value}"])

    segmented = isinstance(uri, list)
    segments_dir = f"{out}_segments"

    if segmented:
        uri = "\n".join([
            f"{url}\n"
            f"\tdir={segments_dir}\n"
            f"\tout={i:08}.mp4"
            for i, url in enumerate(uri)
        ])

    if proxy:
        arguments.append("--all-proxy")
        if proxy.lower().startswith("https://"):
            auth, hostname = proxy[8:].split("@")
            async with start_pproxy(*hostname.split(":"), *auth.split(":")) as pproxy_:
                arguments.extend([pproxy_, "-d"])
                if segmented:
                    arguments.extend([segments_dir, "-i-"])
                    proc = await asyncio.create_subprocess_exec(*arguments, stdin=subprocess.PIPE)
                    await proc.communicate(as_list(uri)[0].encode("utf-8"))
                else:
                    arguments.extend([os.path.dirname(out), uri])
                    proc = await asyncio.create_subprocess_exec(*arguments)
                    await proc.communicate()
        else:
            arguments.append(proxy)

    try:
        if segmented:
            subprocess.run(
                arguments + ["-d", segments_dir, "-i-"],
                input=as_list(uri)[0],
                encoding="utf-8",
                check=True
            )
        else:
            subprocess.run(
                arguments + ["-d", os.path.dirname(out), uri],
                check=True
            )
    except subprocess.CalledProcessError:
        raise ValueError("Aria2c failed too many times, aborting")

    if segmented:
        # merge the segments together
        with open(out, "wb") as ofd:
            for file in sorted(os.listdir(segments_dir)):
                file = os.path.join(segments_dir, file)
                with open(file, "rb") as ifd:
                    data = ifd.read()
                # Apple TV+ needs this done to fix audio decryption
                data = re.sub(b"(tfhd\x00\x02\x00\x1a\x00\x00\x00\x01\x00\x00\x00)\x02", b"\\g<1>\x01", data)
                ofd.write(data)
                os.unlink(file)
        os.rmdir(segments_dir)

    print()


async def saldl(uri, out, headers=None, proxy=None):
    if headers:
        headers.update({k: v for k, v in headers.items() if k.lower() != "accept-encoding"})

    executable = shutil.which("saldl") or shutil.which("saldl-win64") or shutil.which("saldl-win32")
    if not executable:
        raise EnvironmentError("Saldl executable not found...")

    arguments = [
        executable,
        # "--no-status",
        "--skip-TLS-verification",
        "--resume",
        "--merge-in-order",
        "-c8",
        "--auto-size", "1",
        "-D", os.path.dirname(out),
        "-o", os.path.basename(out),
    ]

    if headers:
        arguments.extend([
            "--custom-headers",
            "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
        ])

    if proxy:
        arguments.extend(["--proxy", proxy])

    if isinstance(uri, list):
        raise ValueError("Saldl code does not yet support multiple uri (e.g. segmented) downloads.")
    arguments.append(uri)

    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError:
        raise ValueError("Saldl failed too many times, aborting")

    print()
    
async def tqdm_downloader(uri, out, headers=None, proxy=None):
    proxies = {'https': f"{proxy}"} if 'https://' in proxy else {'http': f"{proxy}"}
    r = requests.get(uri, proxies=proxies, stream=True)
    file_size = int(r.headers["Content-Length"])
    chunk = 1
    chunk_size = 1024
    num_bars = int(file_size / chunk_size)

    with open(out, "wb") as fp:
        for chunk in tqdm.tqdm(
            r.iter_content(chunk_size=chunk_size),
            total=num_bars,
            unit="KB",
            desc=out,
            leave=True,  # progressbar stays
        ):
            fp.write(chunk)
    
    print()

async def m3u8re(uri, out, headers=None, proxy=None):
    out = Path(out)

    if headers:
        headers.update({k: v for k, v in headers.items() if k.lower() != "accept-encoding"})

    executable = shutil.which("m3u8re") or shutil.which("N_m3u8DL-RE")
    if not executable:
        raise EnvironmentError("N_m3u8DL-RE executable not found...")

    if isinstance(uri, list):
        raise ValueError("N_m3u8DL code does not yet support multiple uri (e.g. segmented) downloads.")

    arguments = [
        executable,
        uri,
        "--tmp-dir", str(out.parent),
        "--save-dir", str(out.parent),
        "--save-name", out.name.replace('.mp4','').replace('.vtt','').replace('.m4a',''),
        "--auto-subtitle-fix", "False",
        "--thread-count", "32",
        "--download-retry-count", "100",
        "--log-level", "INFO"
    ]

    if headers:
        arguments.extend([
            "--header",
            "\r\n".join([f"{k}: {v}" for k, v in headers.items()])
        ])
        
    if proxy:
        arguments.extend(["--custom-proxy", proxy])

    try:
        subprocess.run(arguments, check=True)
    except subprocess.CalledProcessError:
        raise ValueError("N_m3u8DL-RE failed too many times, aborting")

    print()