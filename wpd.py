"""Read-only bindings for the subset of Windows Portable Devices used here."""

from contextlib import ExitStack, contextmanager
import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
from datetime import datetime, timedelta
import uuid


class WpdError(OSError):
    def __init__(self, message, hresult=None):
        super().__init__(message)
        self.hresult = hresult


class Guid(c.Structure):
    _fields_ = [("data1", c.c_uint32), ("data2", c.c_uint16),
                ("data3", c.c_uint16), ("data4", c.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PropertyKey(c.Structure):
    _fields_ = [("guid", Guid), ("pid", w.DWORD)]


class VariantData(c.Union):
    _fields_ = [("date", c.c_double), ("filetime", c.c_uint64),
                ("storage", c.c_void_p * 2)]


class PropVariant(c.Structure):
    _fields_ = [("kind", c.c_ushort), ("reserved", c.c_ushort * 3),
                ("value", VariantData)]


def key(guid, pid):
    return PropertyKey(Guid.parse(guid), pid)


# Vtable indices and keys come from PortableDeviceApi.h, PortableDeviceTypes.h
# and PortableDevice.h in the Windows SDK. IStream::Read follows IUnknown.
OBJECT_PROPERTIES = "ef6b490d-5cd8-437a-affc-da8b60ee4a3c"
OBJECT_NAME = key(OBJECT_PROPERTIES, 4)
PERSISTENT_ID = key(OBJECT_PROPERTIES, 5)
CONTENT_TYPE = key(OBJECT_PROPERTIES, 7)
OBJECT_SIZE = key(OBJECT_PROPERTIES, 11)
ORIGINAL_NAME = key(OBJECT_PROPERTIES, 12)
DATE_CREATED = key(OBJECT_PROPERTIES, 18)
DATE_MODIFIED = key(OBJECT_PROPERTIES, 19)
CLIENT_ACCESS = key("204d9f0c-2292-4080-9f42-40664e70f859", 9)
DEFAULT_RESOURCE = key("e81e79be-34f0-41bf-b53f-f1a06ae87842", 0)
CONTAINERS = {
    bytes(Guid.parse("27e2e392-a111-48e0-ab0c-e17705a05f85")),  # Folder
    bytes(Guid.parse("99ed0160-17ff-4c44-9d98-1d7a6f941921")),  # Storage
}
VIDEO = bytes(Guid.parse("9261b03c-3d78-4519-85e3-02c5e1f50bb9"))

PTR = c.c_void_p
PPTR = c.POINTER(PTR)
KEY = c.POINTER(PropertyKey)
DWORD_REF = c.POINTER(w.DWORD)
WCHAR = c.c_wchar_p
OLE = c.WinDLL("ole32")
OLE.CoInitializeEx.argtypes = (PTR, w.DWORD)
OLE.CoInitializeEx.restype = c.c_long
OLE.CoCreateInstance.argtypes = (c.POINTER(Guid), PTR, w.DWORD,
                                c.POINTER(Guid), PPTR)
OLE.CoCreateInstance.restype = c.c_long
OLE.CoTaskMemFree.argtypes = (PTR,)
OLE.CoTaskMemFree.restype = None
OLE.CoUninitialize.restype = None
OLE.PropVariantClear.argtypes = (c.POINTER(PropVariant),)


def check(result, operation):
    if result < 0:
        code = result & 0xFFFFFFFF
        raise WpdError(f"{operation} failed (0x{code:08X})", code)
    return result


def method(pointer, slot, *argtypes, result=c.c_long):
    vtable = c.cast(pointer, c.POINTER(PPTR))[0]
    return c.WINFUNCTYPE(result, PTR, *argtypes)(vtable[slot])


def invoke(pointer, slot, operation, argtypes=(), *args):
    return check(method(pointer, slot, *argtypes)(pointer, *args), operation)


def release(pointer):
    if pointer:
        method(pointer, 2, result=w.ULONG)(pointer)


def create(clsid, iid):
    pointer = PTR()
    check(OLE.CoCreateInstance(c.byref(Guid.parse(clsid)), None, 1,
                               c.byref(Guid.parse(iid)), c.byref(pointer)),
          "Create Windows Portable Devices interface")
    return pointer


def connect_output(pointer, slot, operation):
    output = PTR()
    invoke(pointer, slot, operation, (PPTR,), c.byref(output))
    return output


def get_string(values, prop):
    output = PTR()
    try:
        hr = method(values, 8, KEY, PPTR)(values, c.byref(prop), c.byref(output))
        return c.wstring_at(output) if hr >= 0 and output else None
    finally:
        OLE.CoTaskMemFree(output)


def get_size(values):
    output = c.c_uint64()
    hr = method(values, 14, KEY, c.POINTER(c.c_uint64))(
        values, c.byref(OBJECT_SIZE), c.byref(output))
    return output.value if hr >= 0 else None


def get_date(values, prop):
    output = PropVariant()
    try:
        hr = method(values, 6, KEY, c.POINTER(PropVariant))(
            values, c.byref(prop), c.byref(output))
        if hr >= 0 and output.kind == 7:  # VT_DATE
            return datetime(1899, 12, 30) + timedelta(days=output.value.date)
        if hr >= 0 and output.kind == 64:  # VT_FILETIME
            return datetime(1601, 1, 1) + timedelta(microseconds=output.value.filetime // 10)
    except (ValueError, OverflowError):
        return None
    finally:
        OLE.PropVariantClear(c.byref(output))
    return None


def get_type(values):
    output = Guid()
    invoke(values, 28, "Get WPD content type", (KEY, c.POINTER(Guid)),
           c.byref(CONTENT_TYPE), c.byref(output))
    return bytes(output)


def device_ids(manager):
    count = w.DWORD()
    invoke(manager, 3, "Count WPD devices", (PPTR, DWORD_REF), None, c.byref(count))
    if count.value == 0:
        return []
    ids = (PTR * count.value)()
    try:
        invoke(manager, 3, "List WPD devices", (PPTR, DWORD_REF), ids, c.byref(count))
        return [c.wstring_at(item) for item in ids if item]
    finally:
        for item in ids:
            OLE.CoTaskMemFree(item)


def apple_ids(manager):
    return [item for item in device_ids(manager) if "vid_05ac" in item.lower()]


def attached_phones():
    check(OLE.CoInitializeEx(None, 2), "Initialize Windows COM")
    manager = None
    try:
        manager = create("0af10cec-2ecd-4b92-9581-34f6ae0637f3",
                         "a1567595-4c2f-4574-a6fa-ecef917b9a40")
        return tuple(sorted(apple_ids(manager)))
    finally:
        release(manager)
        OLE.CoUninitialize()


@dataclass(frozen=True)
class MediaObject:
    object_id: str
    name: str
    folders: tuple[str, ...]
    size: int | None
    persistent_id: str | None
    created: datetime | None
    modified: datetime | None
    is_video: bool

    @property
    def source_path(self):
        return "/".join((*self.folders, self.name))


class Iphone:
    def __init__(self, device_id, content, properties, resources, keys):
        self.device_id = device_id
        self.content = content
        self.properties = properties
        self.resources = resources
        self.keys = keys

    def children(self, parent):
        enumerator = PTR()
        invoke(self.content, 3, "Enumerate WPD folder", (w.DWORD, WCHAR, PTR, PPTR),
               0, parent, None, c.byref(enumerator))
        try:
            while True:
                ids = (PTR * 64)()
                count = w.ULONG()
                try:
                    status = invoke(enumerator, 3, "Read WPD folder entries",
                                    (w.ULONG, PPTR, c.POINTER(w.ULONG)),
                                    len(ids), ids, c.byref(count))
                    children = [c.wstring_at(item) for item in ids[:count.value]]
                finally:
                    for item in ids:
                        OLE.CoTaskMemFree(item)
                yield from children
                if status == 1 or not count.value:
                    break
        finally:
            release(enumerator)

    def list_media(self, on_folder=lambda name: None, on_notice=lambda message: None):
        folders = [("DEVICE", ())]
        visited = {"DEVICE"}
        while folders:
            parent, path = folders.pop()
            if path:
                on_folder("/".join(path))
            for object_id in self.children(parent):
                if object_id in visited:
                    continue
                visited.add(object_id)
                values = PTR()
                try:
                    invoke(self.properties, 5, "Read WPD object properties", (WCHAR, PTR, PPTR),
                           object_id, self.keys, c.byref(values))
                except WpdError as error:
                    if parent == "DEVICE" and error.hresult == 0x80042008:
                        on_notice("The driver listed a root storage ID it could not open (0x80042008)")
                        continue
                    raise
                try:
                    kind = get_type(values)
                    name = get_string(values, ORIGINAL_NAME) or get_string(values, OBJECT_NAME)
                    if not name:
                        raise WpdError("An enumerated WPD object has no name")
                    if kind in CONTAINERS:
                        folders.append((object_id, (*path, name)))
                        continue
                    media = MediaObject(object_id, name, path, get_size(values),
                                        get_string(values, PERSISTENT_ID),
                                        get_date(values, DATE_CREATED),
                                        get_date(values, DATE_MODIFIED), kind == VIDEO)
                finally:
                    release(values)
                yield media

    def stream(self, media):
        stream = PTR()
        suggested = w.DWORD()
        invoke(self.resources, 5, f"Open USB stream for {media.name}",
               (WCHAR, KEY, w.DWORD, DWORD_REF, PPTR),
               media.object_id, c.byref(DEFAULT_RESOURCE), 0,  # STGM_READ
               c.byref(suggested), c.byref(stream))
        try:
            capacity = min(suggested.value or 1024 * 1024, 8 * 1024 * 1024)
            buffer = c.create_string_buffer(capacity)
            count = w.ULONG()
            read = method(stream, 3, PTR, w.ULONG, c.POINTER(w.ULONG))
            while True:
                count.value = 0
                status = check(read(stream, buffer, capacity, c.byref(count)),
                               f"Read USB stream for {media.name}")
                if count.value > capacity:
                    raise WpdError("WPD returned an invalid read length")
                if count.value:
                    yield buffer.raw[:count.value]
                if status == 1 or not count.value:
                    break
        finally:
            release(stream)


@contextmanager
def open_iphone():
    check(OLE.CoInitializeEx(None, 2), "Initialize Windows COM")
    with ExitStack() as stack:
        stack.callback(OLE.CoUninitialize)

        def managed(clsid, iid):
            pointer = create(clsid, iid)
            stack.callback(release, pointer)
            return pointer

        manager = managed("0af10cec-2ecd-4b92-9581-34f6ae0637f3",
                          "a1567595-4c2f-4574-a6fa-ecef917b9a40")
        apple_devices = apple_ids(manager)
        if len(apple_devices) != 1:
            raise WpdError("Connect one Apple phone over USB, unlock it, and approve Trust if prompted")
        device_id = apple_devices[0]
        client = managed("0c15d503-d017-47ce-9016-7b3f978721cc",
                         "6848f6f2-3155-4f86-b6f5-263eeeab3143")
        invoke(client, 9, "Set read-only device access", (KEY, w.ULONG),
               c.byref(CLIENT_ACCESS), 0x80000000)  # GENERIC_READ
        device = managed("f7c0039a-4762-488a-b4b3-760ef9a1ba9b",
                         "625e2df8-6392-4cf0-9ad1-3cfa5f17775c")
        invoke(device, 3, "Open Apple USB device read-only. Unlock it and approve Trust if prompted",
               (WCHAR, PTR), device_id, client)
        stack.callback(lambda: method(device, 8)(device))
        content = connect_output(device, 5, "Get WPD content interface")
        stack.callback(release, content)
        properties = connect_output(content, 4, "Get WPD properties interface")
        stack.callback(release, properties)
        resources = connect_output(content, 5, "Get WPD resources interface")
        stack.callback(release, resources)
        keys = managed("de2d022d-2480-43be-97f0-d1fa2cf98f4f",
                       "dada2357-e0ad-492e-98db-dd61c53ba353")
        for prop in (OBJECT_NAME, PERSISTENT_ID, CONTENT_TYPE, OBJECT_SIZE,
                     ORIGINAL_NAME, DATE_CREATED, DATE_MODIFIED):
            invoke(keys, 5, "Add WPD property to read list", (KEY,), c.byref(prop))
        yield Iphone(device_id, content, properties, resources, keys)
