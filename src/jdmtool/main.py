import argparse 
from functools import wraps
from getpass import getpass
import os
import pathlib
import shutil
import sys
import typing as T
import zipfile

import tqdm
import usb1

from .chartview import ChartView
from .device import GarminProgrammerDevice, GarminProgrammerException
from .downloader import Downloader, DownloaderException
from .gplugin import GPlugin, GPluginException
from .service import ChartViewService, Service, SimpleService, ServiceException, get_data_dir, load_services


CARD_TYPE_SD = 2
CARD_TYPE_GARMIN = 7

LDR_SYS = 'ldr_sys'
GRM_FEAT_KEY = 'grm_feat_key.zip'
FEAT_UNLK = 'feat_unlk.dat'

DB_MAGIC = (
    b'\xeb<\x90GARMIN10\x00\x02\x08\x01\x00\x01\x00\x02\x00\x80\xf0\x10\x00?\x00\xff\x00?\x00\x00\x00'
    b'\x00\x00\x00\x00\x00\x00)\x02\x11\x00\x00GARMIN AT  FAT16   \x00\x00'
)

MAX_SIZE = len(GarminProgrammerDevice.DATA_PAGES) * 16 * 0x1000


DETAILED_INFO_MAP = [
    ("Aircraft Manufacturer", "oracle_aircraft_manufacturer"),
    ("Aircraft Model", "oracle_aircraft_model"),
    ("Aircraft Tail Number", "oracle_aircraft_tail_number"),
    (None, None),
    ("Avionics", "avionics"),
    ("Service Type", "service_type"),
    ("Coverage", "coverage_desc"),
    ("Service Renewal Date", "service_renewal_date"),
    (None, None),
    ("Service Code", "service_code"),
    ("Service ID", "unique_service_id"),
    ("Serial Number", "serial_number"),
    ("System ID", "avionics_id"),
    (None, None),
    ("Version", "display_version"),
    ("Version Start Date", "version_start_date"),
    ("Version End Date", "version_end_date"),
    (None, None),
    ("Next Version", "next_display_version"),
    ("Next Version Available Date", "next_version_avail_date"),
    ("Next Version Start Date", "next_version_start_date"),
]


def with_usb(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        with usb1.USBContext() as usbcontext:
            try:
                usbdev = usbcontext.getByVendorIDAndProductID(GarminProgrammerDevice.VID, GarminProgrammerDevice.PID)
                if usbdev is None:
                    raise GarminProgrammerException("Device not found")

                print(f"Found device: {usbdev}")
                handle = usbdev.open()
            except usb1.USBError as ex:
                raise GarminProgrammerException(f"Could not open: {ex}")

            with handle.claimInterface(0):
                handle.resetDevice()
                dev = GarminProgrammerDevice(handle)
                dev.init()
                f(dev, *args, **kwargs)

    return wrapper


def _loop_helper(dev, i):
    dev.set_led(i % 2 == 0)
    if not dev.has_card():
        dev.set_led(False)
        raise GarminProgrammerException("Card not found!")


def cmd_login() -> None:
    downloader = Downloader()

    username = input("Username: ")
    password = getpass("Password: ")

    downloader.login(username, password)
    print("Logged in successfully")

def cmd_refresh() -> None:
    downloader = Downloader()
    print("Downloading services...")
    downloader.refresh()
    print("Downloading keychain...")
    downloader.refresh_keychain()
    print("Success")

def cmd_list() -> None:
    services = load_services()

    row_format = "{:>2}  {:<70}  {:<25}  {:<8}  {:<10}  {:<10}  {:<10}"

    header = row_format.format("ID", "Name", "Coverage", "Version", "Start Date", "End Date", "Downloaded")
    print(f'\033[1m{header}\033[0m')
    for idx, service in enumerate(services):
        avionics: str = service.get_property('avionics')
        service_type: str = service.get_property('service_type')
        name = f'{avionics} - {service_type}'
        coverage: str = service.get_property('coverage_desc')
        if len(coverage) > 25:
            coverage = coverage[:24] + '…'
        version: str = service.get_property('display_version')
        start_date: str = service.get_property('version_start_date').split()[0]
        end_date: str = service.get_property('version_end_date').split()[0]

        downloaded = all(f.exists() for f in service.get_download_paths())

        print(row_format.format(idx, name, coverage, version, start_date, end_date, 'Y' if downloaded else ''))

def cmd_info(id: int) -> None:
    services = load_services()
    if id < 0 or id >= len(services):
        raise DownloaderException("Invalid download ID")

    service = services[id]

    for desc, path in DETAILED_INFO_MAP:
        if desc is None:
            print()
        else:
            value = service.get_optional_property(path) or ''
            print(f'{desc+":":<30}{value}')

    download_paths = service.get_download_paths()

    print()
    print("Downloads:")
    for f in download_paths:
        status = '' if f.exists() else '  (missing)'
        print(f'  {f}{status}')

def cmd_download(id: int) -> None:
    downloader = Downloader()

    services = load_services()
    if id < 0 or id >= len(services):
        raise DownloaderException("Invalid download ID")

    service = services[id]

    databases = service.get_databases()
    sffs = service.get_sffs()
    oems = service.get_oems()

    for database in databases:
        if database.dest_path.exists():
            print(f"Skipping {database.dest_path}: already exists")
            continue

        database.dest_path.parent.mkdir(parents=True, exist_ok=True)

        with tqdm.tqdm(desc=f"Downloading {database.dest_path.name}", total=database.size, unit='B', unit_scale=True) as t:
            def _update(n: int) -> None:
                t.update(n)

            downloader.download_database(database.params, database.dest_path, database.crc32, _update)

        print(f"Downloaded to {database.dest_path}")

    for sff in sffs:
        if sff.dest_path.exists():
            print(f"Skipping {sff.dest_path}: already exists")
            continue

        sff.dest_path.parent.mkdir(parents=True, exist_ok=True)

        print(f'Downloading {sff.dest_path.name}...')
        downloader.download_sff(sff.params, sff.dest_path)
        print(f"Downloaded to {sff.dest_path}")

    for oem in oems:
        if oem.dest_path.exists():
            print(f"Skipping {oem.dest_path}: already exists")
            continue

        oem.dest_path.parent.mkdir(parents=True, exist_ok=True)

        print(f'Downloading {oem.dest_path.name}...')
        downloader.download_oem(oem.params, oem.dest_path)
        print(f"Downloaded to {oem.dest_path}")

def _transfer_sd_card(service: Service, path: pathlib.Path):
    if not all(f.exists() for f in service.get_download_paths()):
        raise DownloaderException("Need to download it first")

    databases = service.get_databases()
    sffs = service.get_sffs()

    if path.is_block_device():
        raise DownloaderException(f"{path} is a device file; need the directory where the SD card is mounted")

    if not path.is_dir():
        raise DownloaderException(f"{path} is not a directory")

    if isinstance(path, pathlib.PosixPath):
        if not path.is_mount():
            print(f"WARNING: {path} appears to be a normal directory, not a device.")
    else:
        if path.parent != path:
            print(f"WARNING: {path} appears to be a directory, not a drive.")
        elif not path.root:
            path = path / '/'

    feat_unlk = False

    media_list = service.get_media()
    for media in media_list:
        assert int(media.findtext('./card_type', '')) == CARD_TYPE_SD

        filename = media.findtext('./filename')

        if filename == FEAT_UNLK:
            feat_unlk = True

    if feat_unlk:
        print(f"NOTE: this database requires {FEAT_UNLK}; will need to run g_plugin from JDM!")

    prompt = input(f"Transfer databases to {path}? (y/n) ")
    if prompt.lower() != 'y':
        raise DownloaderException("Cancelled")

    if isinstance(service, SimpleService):
        assert len(databases) == 1, databases
        database_path = databases[0].dest_path

        g_plugin = None
        if feat_unlk:
            g_plugin = GPlugin(path)
            g_plugin.init()

        target_filenames = []
        with zipfile.ZipFile(database_path) as database_zip:
            infolist = database_zip.infolist()
            for info in infolist:
                info.filename = info.filename.replace('\\', '/')  # 🤦‍
                target = path / info.filename
                target_filenames.append(info.filename)
                print(f"Copying {database_path}!{info.orig_filename} to {target}...")
                database_zip.extract(info, path)

        if feat_unlk:
            garmin_sec_id = service.get_property('garmin_sec_id')
            garmin_system_ids = service.get_property('garmin_system_ids')
            unique_service_id = service.get_property('unique_service_id')
            version = service.get_property('version')

            assert g_plugin
            try:
                print("Starting g_plugin...")
                g_plugin.start()

                for target in target_filenames:
                    print(f"Updating {FEAT_UNLK} for {target}...")
                    g_plugin.run(
                        target, FEAT_UNLK, garmin_sec_id, garmin_system_ids, unique_service_id, version
                    )
            finally:
                print("Shutting down g_plugin.")
                g_plugin.stop()
    elif isinstance(service, ChartViewService):
        if feat_unlk:
            raise DownloaderException(f"{FEAT_UNLK} not supported for ChartView!")

        chartview = ChartView([cfg.dest_path for cfg in service.get_databases()])
        try:
            chartview.transfer(path)
        finally:
            chartview.close()

        assert False

    for sff in sffs:
        sff_target = path / sff.dest_path.name
        print(f"Copying {sff.dest_path} to {sff_target}...")
        shutil.copy(sff.dest_path, sff_target)

    if sffs:
        keychain_source = get_data_dir() / GRM_FEAT_KEY
        (path / LDR_SYS).mkdir(exist_ok=True)
        keychain_target = path / LDR_SYS / GRM_FEAT_KEY
        print(f"Copying {keychain_source} to {keychain_target}...")
        shutil.copy(keychain_source, keychain_target)

def _transfer_garmin_impl(dev: GarminProgrammerDevice, service: Service):
    databases = service.get_databases()
    assert len(databases) == 1, databases

    version = service.get_property('version')
    unique_service_id = service.get_property('unique_service_id')

    path = databases[0].dest_path
    if not path.exists():
        raise DownloaderException("Need to download it first")

    new_metadata = f'{{{version}~{unique_service_id}}}'  # E.g. {2303~12345678}

    prompt = input(f"Transfer {path} to the data card? (y/n) ")
    if prompt.lower() != 'y':
        raise DownloaderException("Cancelled")

    _clear_metadata(dev)
    _write_database(dev, str(path))

    print(f"Writing new metadata: {new_metadata}")
    _write_metadata(dev, new_metadata)

# Pylance workaround
_transfer_garmin = with_usb(_transfer_garmin_impl)


def cmd_transfer(id: int, device: T.Optional[str]) -> None:
    services = load_services()
    if id < 0 or id >= len(services):
        raise DownloaderException("Invalid download ID")

    service = services[id]

    card_type = int(service.get_property('media/card_type'))

    if card_type == CARD_TYPE_SD:
        if not device:
            raise DownloaderException("This database requires a path to an SD card")

        _transfer_sd_card(service, pathlib.Path(device))
    elif card_type == CARD_TYPE_GARMIN:
        if device:
            raise DownloaderException("This database requires a programmer device and does not support paths")

        _transfer_garmin(service)

    print("Done")

@with_usb
def cmd_detect(dev: GarminProgrammerDevice) -> None:
    version = dev.get_version()
    print(f"Firmware version: {version}")
    if dev.has_card():
        print("Card inserted:")
        iid = dev.get_iid()
        print(f"  IID: 0x{iid:x}")
        unknown = dev.get_unknown()
        print(f"  Unknown identifier: 0x{unknown:x}")
    else:
        print("No card")

@with_usb
def cmd_read_metadata(dev: GarminProgrammerDevice) -> None:
    dev.before_read()
    dev.select_page(GarminProgrammerDevice.METADATA_PAGE)
    blocks = []
    for i in range(16):
        _loop_helper(dev, i)
        blocks.append(dev.read_block())
    value = b''.join(blocks).rstrip(b"\xFF").decode()
    print(f"Database metadata: {value}")

def _clear_metadata(dev: GarminProgrammerDevice) -> None:
    dev.before_write()
    dev.select_page(GarminProgrammerDevice.METADATA_PAGE)
    dev.erase_page()

def _write_metadata(dev: GarminProgrammerDevice, metadata: str) -> None:
    dev.before_write()
    page = metadata.encode().ljust(0x10000, b'\xFF')

    dev.select_page(GarminProgrammerDevice.METADATA_PAGE)

    # Data card can only write by changing 1s to 0s (effectively doing a bit-wise AND with
    # the existing contents), so all data needs to be "erased" first to reset everything to 1s.
    dev.erase_page()

    for i in range(16):
        _loop_helper(dev, i)

        block = page[i*0x1000:(i+1)*0x1000]

        dev.write_block(block)

@with_usb
def cmd_write_metadata(dev: GarminProgrammerDevice, metadata: str) -> None:
    _write_metadata(dev, metadata)
    print("Done")

@with_usb
def cmd_read_database(dev: GarminProgrammerDevice, path: str) -> None:
    with open(path, 'w+b') as fd:
        with tqdm.tqdm(desc="Reading the database", total=MAX_SIZE, unit='B', unit_scale=True) as t:
            dev.before_read()
            for i in range(len(GarminProgrammerDevice.DATA_PAGES) * 16):
                _loop_helper(dev, i)

                if i % 256 == 0:
                    dev.select_page(GarminProgrammerDevice.DATA_PAGES[i // 16])

                block = dev.read_block()

                if block == b'\xFF' * 0x1000:
                    break

                fd.write(block)
                t.update(len(block))

        # Garmin card has no concept of size of the data,
        # so we need to remove the trailing "\xFF"s.
        print("Truncating the file...")
        fd.seek(0, os.SEEK_END)
        pos = fd.tell()
        while pos > 0:
            pos -= 1024
            fd.seek(pos)
            block = fd.read(1024)
            if block != b'\xFF' * 1024:
                break
        fd.truncate()

    print("Done")

def _write_database(dev: GarminProgrammerDevice, path: str) -> None:
    with open(path, 'rb') as fd:
        size = os.fstat(fd.fileno()).st_size

        if size > MAX_SIZE:
            raise GarminProgrammerException(f"Database file is too big! The maximum size is {MAX_SIZE}.")

        pages_required = min(size // 16 // 0x1000 + 3, len(GarminProgrammerDevice.DATA_PAGES))
        page_ids = GarminProgrammerDevice.DATA_PAGES[:pages_required]
        total_size = pages_required * 16 * 0x1000

        magic = fd.read(64)
        if magic != DB_MAGIC:
            raise GarminProgrammerException(f"Does not look like a Garmin database file.")

        fd.seek(0)

        dev.before_write()

        # Data card can only write by changing 1s to 0s (effectively doing a bit-wise AND with
        # the existing contents), so all data needs to be "erased" first to reset everything to 1s.
        with tqdm.tqdm(desc="Erasing the database", total=total_size, unit='B', unit_scale=True) as t:
            for i, page_id in enumerate(page_ids):
                _loop_helper(dev, i)
                dev.select_page(page_id)
                dev.erase_page()
                t.update(16 * 0x1000)

        with tqdm.tqdm(desc="Writing the database", total=total_size, unit='B', unit_scale=True) as t:
            for i in range(pages_required * 16):
                block = fd.read(0x1000).ljust(0x1000, b'\xFF')

                _loop_helper(dev, i)

                if i % 256 == 0:
                    dev.select_page(page_ids[i // 16])

                dev.write_block(block)
                t.update(len(block))

        fd.seek(0)

        with tqdm.tqdm(desc="Verifying the database", total=total_size, unit='B', unit_scale=True) as t:
            dev.before_read()
            for i in range(pages_required * 16):
                file_block = fd.read(0x1000).ljust(0x1000, b'\xFF')

                _loop_helper(dev, i)

                if i % 256 == 0:
                    dev.select_page(page_ids[i // 16])

                card_block = dev.read_block()

                if card_block != file_block:
                    raise GarminProgrammerException(f"Verification failed! Block {i} is incorrect.")

                t.update(len(file_block))

@with_usb
def cmd_write_database(dev: GarminProgrammerDevice, path: str) -> None:
    prompt = input(f"Transfer {path} to the data card? (y/n) ")
    if prompt.lower() != 'y':
        raise DownloaderException("Cancelled")

    try:
        _write_database(dev, path)
    except IOError as ex:
        raise GarminProgrammerException(f"Could not read the database file: {ex}")

    print("Done")

def main():
    parser = argparse.ArgumentParser(description="Program a Garmin data card")

    subparsers = parser.add_subparsers(metavar="<command>")
    subparsers.required = True

    login_p = subparsers.add_parser(
        "login",
        help="Log into Jeppesen",
    )
    login_p.set_defaults(func=cmd_login)

    refresh_p = subparsers.add_parser(
        "refresh",
        help="Refresh the list available downloads",
    )
    refresh_p.set_defaults(func=cmd_refresh)

    list_p = subparsers.add_parser(
        "list",
        help="Show the (cached) list of available downloads",
    )
    list_p.set_defaults(func=cmd_list)

    info_p = subparsers.add_parser(
        "info",
        help="Show detailed info about the download",
    )
    info_p.add_argument(
        "id",
        help="ID of the download",
        type=int,
    )
    info_p.set_defaults(func=cmd_info)

    download_p = subparsers.add_parser(
        "download",
        help="Download the data",
    )
    download_p.add_argument(
        "id",
        help="ID of the download",
        type=int,
    )
    download_p.set_defaults(func=cmd_download)

    transfer_p = subparsers.add_parser(
        "transfer",
        help="Transfer the downloaded database to an SD card or a Garmin data card",
    )
    transfer_p.add_argument(
        "id",
        help="ID of the download",
        type=int,
    )
    transfer_p.add_argument(
        "device",
        help="SD card directory (only for G1000 databases)",
        type=str,
        nargs='?',
    )
    transfer_p.set_defaults(func=cmd_transfer)

    detect_p = subparsers.add_parser(
        "detect",
        help="Detect a card programmer device",
    )
    detect_p.set_defaults(func=cmd_detect)

    read_metadata_p = subparsers.add_parser(
        "read-metadata",
        help="Read the database metadata",
    )
    read_metadata_p.set_defaults(func=cmd_read_metadata)

    write_metadata_p = subparsers.add_parser(
        "write-metadata",
        help="Write the database metadata",
    )
    write_metadata_p.add_argument(
        "metadata",
        help="Metadata string, e.g. {2303~12345678}",
    )
    write_metadata_p.set_defaults(func=cmd_write_metadata)

    read_database_p = subparsers.add_parser(
        "read-database",
        help="Read the database from the card and write to the file",
    )
    read_database_p.add_argument(
        "path",
        help="File to write the database to",
    )
    read_database_p.set_defaults(func=cmd_read_database)

    write_database_p = subparsers.add_parser(
        "write-database",
        help="Write the database to the card",
    )
    write_database_p.add_argument(
        "path",
        help="Database file, e.g. dgrw72_2302_742ae60e.bin",
    )
    write_database_p.set_defaults(func=cmd_write_database)

    args = parser.parse_args()

    kwargs = vars(args)
    func = kwargs.pop('func')

    try:
        func(**kwargs)
    except DownloaderException as ex:
        print(ex)
        return 1
    except ServiceException as ex:
        print(ex)
        return 1
    except GPluginException as ex:
        print(ex)
        return 1
    except GarminProgrammerException as ex:
        print(ex)
        return 1

    return 0
