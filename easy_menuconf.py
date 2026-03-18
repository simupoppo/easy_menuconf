#!/usr/bin/env python3
"""
easy_menuconf.py - GUI editor for Simutrans menuconf.tab

Dependencies:
    sudo apt-get install python3-tk
    pip install Pillow  (optional, for faster image rendering)

Usage:
    python3 easy_menuconf.py [menuconf.tab]
"""

import os
import re
import struct
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# Try to import PIL for faster image handling
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ─────────────────────────────────────────────────────────────────────────────
# PAK FILE READER
# ─────────────────────────────────────────────────────────────────────────────

# Simutrans special color table (player colors, lights, non-darkening greys …)
_SPECIAL_COLORS = [
    0x244B67, 0x395E7C, 0x4C7191, 0x6084A7, 0x7497BD, 0x88ABD3, 0x9CBEE9, 0xB0D2FF,  # Player color 1
    0x7B5803, 0x8E6F04, 0xA18605, 0xB49D07, 0xC6B408, 0xD9CB0A, 0xECE20B, 0xFFF90D,  # Player color 2
    0x57656F, 0x7F9BF1, 0xFFFF53, 0xFF211D, 0x01DD01,                                  # special lights
    0x6B6B6B, 0x9B9B9B, 0xB3B3B3, 0xC9C9C9, 0xDFDFDF,                                 # non-darkening greys
    0xE3E3FF, 0xC1B1D1, 0x4D4D4D, 0xFF017F, 0x0101FF,                                  # misc lights
]


def _nodeval_to_rgba(intval):
    """
    Convert a 16-bit pak pixel word to (r, g, b, a).
    Ranges:
      0x0000–0x7FFF  : RGB565 opaque
      0x8000–0x801F  : special color (opaque)
      0x8020–0x83F0  : special color with alpha
      0x83F1+        : semi-transparent direct-color pixel
    """
    if intval < 0x8000:
        r = (intval >> 11) & 0x1F;  r = (r << 3) | (r >> 2)
        g = (intval >>  5) & 0x3F;  g = (g << 2) | (g >> 4)
        b =  intval        & 0x1F;  b = (b << 3) | (b >> 2)
        return r, g, b, 255
    elif intval >= 0x8020 + 31 * 31:
        a  = 255 - (30 - (intval - 0x8020) % 31) * 8
        pv = (intval - 0x8020 - 31 * 31) // 31
        r  = (pv & 0b0000001110000000) >> 2
        g  = (pv & 0b0000000001111000) << 1
        b  = (pv & 0b0000000000000111) << 5
        return r, g, b, a
    else:
        if intval < 0x8020:
            special_int = intval - 0x8000
            a = 255
        else:
            a = 255 - (30 - (intval - 0x8020) % 31) * 8
            special_int = (intval - 0x8020) // 31
        sc = _SPECIAL_COLORS[special_int] if special_int < len(_SPECIAL_COLORS) else 0
        return (sc >> 16) & 0xFF, (sc >> 8) & 0xFF, sc & 0xFF, a


def _decode_img_pixels(node_data):
    """
    Decode a Simutrans IMG node payload to an RGBA pixel list.
    Returns (w, h, pixels) where pixels is a list of rows of (r,g,b,a) tuples,
    or None on error.

    Version detection:
      ver=0 : header is 12 bytes; stored pixel count at offset 4 satisfies count*2+12==len
      ver=1,2: header is 10 bytes; x_width/y_width are 1-byte each
      ver=3  : header is 10 bytes; x_width at [4:6], y_width at [7:9] (both u16)

    RLE state machine (4 states):
      0 = read initial transparent count for this row
      1 = read subsequent transparent count (word==0 ends row)
      2 = read colored-pixel count (word==0 → back to state 1; else count=word&0x7FFF → state 3)
      3 = read pixel words (count times) → state 1
    """
    if len(node_data) < 10:
        return None

    # Version 0 detection: pixel count stored at offset 4 as uint32
    if len(node_data) >= 12:
        stored_count = struct.unpack_from('<I', node_data, 4)[0]
        if stored_count * 2 + 12 == len(node_data):
            ver     = 0
            x_min   = node_data[0]
            x_width = node_data[1]
            y_min   = node_data[2]
            y_width = node_data[3]
            imglen  = stored_count
            pixel_start = 12
        else:
            ver = node_data[6]
            stored_count = -1  # not ver0
    else:
        ver = node_data[6]
        stored_count = -1

    if stored_count < 0:  # ver 1/2/3
        if ver <= 2:
            x_min   = struct.unpack_from('<h', node_data, 0)[0]
            y_min   = struct.unpack_from('<h', node_data, 2)[0]
            x_width = node_data[4]
            y_width = node_data[5]
        else:  # ver == 3
            x_min   = struct.unpack_from('<h', node_data, 0)[0]
            y_min   = struct.unpack_from('<h', node_data, 2)[0]
            x_width = struct.unpack_from('<H', node_data, 4)[0]
            y_width = struct.unpack_from('<H', node_data, 7)[0]
        pixel_start = 10
        imglen = (len(node_data) - 10) // 2

    if x_width <= 0 or y_width <= 0 or x_width > 512 or y_width > 512:
        return None

    raw   = node_data[pixel_start:]
    nw    = min(imglen, len(raw) // 2)
    words = struct.unpack_from(f'<{nw}H', raw)

    # 4-state RLE decode → flat pixel list
    state             = 0   # 0=init-transp 1=subseq-transp 2=color-count 3=color-pixels
    temp_cornum       = 0
    temp_colorpixels  = 0
    flat              = []  # list of (r,g,b,a)

    for word in words:
        if state == 0 or state == 1:
            if word == 0x8000:
                state = 2
                continue
            for _ in range(word):
                flat.append((0, 0, 0, 0))
            temp_cornum += word
            if word == 0 and state == 1:
                # end of row: fill remaining transparent if ver>=2
                if ver >= 2:
                    for _ in range(x_width - temp_cornum):
                        flat.append((0, 0, 0, 0))
                temp_cornum = 0
                state = 0
            else:
                state = 2
            continue
        if state == 2:
            if word > 0:
                temp_colorpixels = word & 0x7FFF
                state = 3
            else:
                state = 1
            continue
        if state == 3:
            flat.append(_nodeval_to_rgba(word))
            temp_cornum += 1
            temp_colorpixels -= 1
            if temp_colorpixels == 0:
                state = 1

    # For ver<2 x_width is computed from data (x_min is always 0)
    if ver < 2 and y_width > 0:
        x_min   = 0
        x_width = len(flat) // y_width

    if x_width <= 0 or y_width <= 0:
        return None

    # Pad/trim flat list to exactly x_width*y_width
    expected = x_width * y_width
    if len(flat) < expected:
        flat.extend([(0, 0, 0, 0)] * (expected - len(flat)))

    pixels = [flat[r * x_width:(r + 1) * x_width] for r in range(y_width)]
    return x_width, y_width, pixels


class PakFile:
    """Reads a .pak file and extracts all IMG images in order."""

    def __init__(self, path):
        self.images = []  # list of (w, h, pixels)
        self._load(path)

    def _load(self, path):
        try:
            with open(path, 'rb') as f:
                data = f.read()
        except OSError:
            return

        # Skip text header until 0x1A
        i = 0
        while i < len(data) and data[i] != 0x1A:
            i += 1
        if i >= len(data):
            return
        i += 1  # skip 0x1A

        if i + 4 > len(data):
            return
        # version (not used for decoding here)
        i += 4

        self._parse_nodes(data, i)

    def _read_node_header(self, data, i):
        """Returns (tag, children, size, header_size) or None."""
        if i + 8 > len(data):
            return None
        tag      = data[i:i+4]
        children = struct.unpack_from('<H', data, i+4)[0]
        sz_u16   = struct.unpack_from('<H', data, i+6)[0]
        if sz_u16 == 0xFFFF:
            if i + 12 > len(data):
                return None
            sz  = struct.unpack_from('<I', data, i+8)[0]
            hdr = 12
        else:
            sz  = sz_u16
            hdr = 8
        return tag, children, sz, hdr

    def _parse_nodes(self, data, i, n=65536):
        """Walk n sibling nodes at offset i, recursing to collect all images.

        File layout: each node = header + payload + children (depth-first).
        IMG1/IMG2 nodes contain image children (IMG\\x00 leaves).
        All other nodes are containers — recurse into them.
        """
        for _ in range(n):
            if i + 8 > len(data):
                break
            result = self._read_node_header(data, i)
            if result is None:
                break
            tag, children, sz, hdr = result
            children_start = i + hdr + sz

            if tag == b'IMG1' or tag == b'IMG2':
                # Each child is one image leaf (IMG\x00)
                j = children_start
                for _ in range(children):
                    r2 = self._read_node_header(data, j)
                    if r2 is None:
                        break
                    ctag, cch, csz, chdr = r2
                    if ctag[:3] == b'IMG':
                        img = _decode_img_pixels(data[j+chdr : j+chdr+csz])
                        self.images.append(img)
                    j += chdr + csz + self._children_total_size(data, j+chdr+csz, cch)
            else:
                # Container node — recurse into its children
                self._parse_nodes(data, children_start, children)

            i += hdr + sz + self._children_total_size(data, children_start, children)

    def _children_total_size(self, data, i, n):
        total = 0
        for _ in range(n):
            result = self._read_node_header(data, i)
            if result is None:
                break
            tag, children, sz, hdr = result
            child_sz = hdr + sz + self._children_total_size(data, i+hdr+sz, children)
            i += child_sz
            total += child_sz
        return total

    def get_image(self, idx):
        """Return (w, h, pixels) for icon at index idx, or None."""
        if 0 <= idx < len(self.images):
            return self.images[idx]
        return None


class PakImageCache:
    """Manages multiple pak files and builds tkinter PhotoImages."""

    def __init__(self):
        self.pak_files = {}        # pak_name -> PakFile
        self.tk_images  = {}       # (pak_name, idx) -> tk.PhotoImage
        self.icon_size  = 32

    def load_pak_dir(self, pak_dir, icon_size=32):
        self.pak_files.clear()
        self.tk_images.clear()
        self.icon_size = icon_size
        PAK_NAMES = [
            'menu.GeneralTools', 'menu.SimpleTools', 'menu.DialogeTools',
            'menu.BarTools', 'menu.RailTools', 'menu.RoadTools',
            'menu.ShipTools', 'menu.ListTools',
        ]
        for name in PAK_NAMES:
            path = os.path.join(pak_dir, name + '.pak')
            if os.path.exists(path):
                self.pak_files[name] = PakFile(path)
        # Also load any other menu.*.pak files
        try:
            for fname in os.listdir(pak_dir):
                if fname.startswith('menu.') and fname.endswith('.pak'):
                    name = fname[:-4]
                    if name not in self.pak_files:
                        self.pak_files[name] = PakFile(os.path.join(pak_dir, fname))
        except OSError:
            pass

    def _pixels_to_photo(self, w, h, pixels):
        """Convert pixel list to tkinter PhotoImage."""
        sz = self.icon_size
        # Scale pixels to icon_size
        scale_x = w / sz if sz > 0 else 1
        scale_y = h / sz if sz > 0 else 1
        if HAS_PIL:
            img = Image.new('RGB', (w, h))
            for y, row in enumerate(pixels):
                for x, (r, g, b, a) in enumerate(row):
                    img.putpixel((x, y), (r, g, b))
            img = img.resize((sz, sz), Image.NEAREST)
            return ImageTk.PhotoImage(img)
        else:
            photo = tk.PhotoImage(width=sz, height=sz)
            rows_data = []
            for sy in range(sz):
                py = min(int(sy * scale_y), h-1)
                row_str = ' '.join(
                    '#{:02x}{:02x}{:02x}'.format(*pixels[py][min(int(sx*scale_x), w-1)][:3])
                    for sx in range(sz)
                )
                rows_data.append('{' + row_str + '}')
            photo.put(' '.join(rows_data))
            return photo

    def get_tk_image(self, pak_name, idx):
        """Get cached tk.PhotoImage for pak_name[idx]."""
        key = (pak_name, idx)
        if key in self.tk_images:
            return self.tk_images[key]
        pf = self.pak_files.get(pak_name)
        if pf is None:
            return None
        img = pf.get_image(idx)
        if img is None:
            return None
        w, h, pixels = img
        photo = self._pixels_to_photo(w, h, pixels)
        self.tk_images[key] = photo
        return photo

    def get_icon_for_tool(self, tool_type, icon_num, icon_size):
        """Get tk image for a tool definition."""
        PAK_MAP = {
            'general_tool': 'menu.GeneralTools',
            'simple_tool':  'menu.SimpleTools',
            'dialog_tool':  'menu.DialogeTools',
            'toolbar':      'menu.BarTools',
        }
        pak_name = PAK_MAP.get(tool_type, 'menu.ToolbarTools')
        return self.get_tk_image(pak_name, icon_num)


# ─────────────────────────────────────────────────────────────────────────────
# MENUCONF PARSER
# ─────────────────────────────────────────────────────────────────────────────

class ToolEntry:
    """Represents one tool definition: general_tool[i]=icon,cursor,sound,key etc."""
    def __init__(self, tool_type, index, icon, extra_fields, comment=''):
        self.tool_type    = tool_type    # 'general_tool', 'simple_tool', 'dialog_tool'
        self.index        = index        # int
        self.icon         = icon         # str (may be empty)
        self.extra_fields = extra_fields # list of str (cursor, sound, key / key for simple&dialog)
        self.comment      = comment      # inline comment

    @property
    def icon_num(self):
        try:
            return int(self.icon)
        except (ValueError, TypeError):
            return -1

    @property
    def key(self):
        if self.tool_type == 'general_tool' and len(self.extra_fields) >= 3:
            return self.extra_fields[2]
        elif self.tool_type in ('simple_tool', 'dialog_tool') and len(self.extra_fields) >= 1:
            return self.extra_fields[0]
        return ''

    def to_string(self):
        fields = [self.icon] + self.extra_fields
        # Strip trailing empty fields
        while fields and fields[-1] == '':
            fields.pop()
        value = ','.join(fields)
        line = f'{self.tool_type}[{self.index}]={value}'
        if self.comment:
            line += f' {self.comment}'
        return line


class ToolbarEntry:
    """Represents one toolbar slot: toolbar[t][i]=ref,icon,key,param,helpfile."""
    def __init__(self, toolbar_id, slot, ref, icon='', key='', param='', helpfile='', comment=''):
        self.toolbar_id = toolbar_id  # int
        self.slot       = slot        # int
        self.ref        = ref         # str e.g. 'general_tool[0]', 'toolbar[1]', '-', 'ways(2,0)'
        self.icon       = icon        # str override icon num
        self.key        = key         # str override key
        self.param      = param       # str default parameter
        self.helpfile   = helpfile    # str helpfile (only for toolbar refs)
        self.comment    = comment

    @property
    def icon_num(self):
        try:
            return int(self.icon)
        except (ValueError, TypeError):
            return -1

    @property
    def ref_type(self):
        """Return the tool type of the reference."""
        m = re.match(r'(\w+)\[', self.ref)
        if m:
            return m.group(1)
        # special refs: ways(x,y), signs(x), bridges(x), buildings(x,y), etc.
        m2 = re.match(r'(\w+)\(', self.ref)
        if m2:
            return m2.group(1)
        if self.ref == '-':
            return 'separator'
        return 'unknown'

    @property
    def ref_index(self):
        """Return the index of the referenced tool (for general/simple/dialog/toolbar)."""
        m = re.match(r'\w+\[(\d+)\]', self.ref)
        if m:
            return int(m.group(1))
        return -1

    def to_string(self):
        fields = [self.ref, self.icon, self.key, self.param, self.helpfile]
        # Strip trailing empty fields
        while len(fields) > 1 and fields[-1] == '':
            fields.pop()
        value = ','.join(fields)
        line = f'toolbar[{self.toolbar_id}][{self.slot}]={value}'
        if self.comment:
            line += f' {self.comment}'
        return line


class MenuconfFile:
    """Parse and write menuconf.tab, preserving comments and structure."""

    def __init__(self):
        self.lines        = []         # raw lines (for reconstruction)
        self.icon_width   = 32
        self.icon_height  = 32
        self.general_tools = {}        # int -> ToolEntry
        self.simple_tools  = {}        # int -> ToolEntry
        self.dialog_tools  = {}        # int -> ToolEntry
        self.toolbars      = {}        # int -> {int -> ToolbarEntry}
        self.filepath      = None

    def load(self, path):
        self.filepath = path
        self.lines = []
        self.general_tools.clear()
        self.simple_tools.clear()
        self.dialog_tools.clear()
        self.toolbars.clear()

        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            raw = f.readlines()

        self.lines = raw
        for line in raw:
            self._parse_line(line)

    def _parse_line(self, line):
        # Strip inline comment
        content = line.strip()
        if not content or content.startswith('#'):
            return

        # Split at '#' to get comment (but beware of '#' in values)
        comment = ''
        if ' #' in content:
            idx = content.index(' #')
            comment = content[idx+1:]
            content = content[:idx].strip()

        # icon_width / icon_height
        m = re.match(r'(icon_width|icon_height)\s*=\s*(\d+)', content)
        if m:
            if m.group(1) == 'icon_width':
                self.icon_width = int(m.group(2))
            else:
                self.icon_height = int(m.group(2))
            return

        # general_tool[i]=...
        m = re.match(r'(general_tool|simple_tool|dialog_tool)\[(\d+)\]=(.*)$', content)
        if m:
            tool_type = m.group(1)
            idx = int(m.group(2))
            fields = m.group(3).split(',')
            icon = fields[0] if fields else ''
            extra = fields[1:] if len(fields) > 1 else []
            entry = ToolEntry(tool_type, idx, icon, extra, comment)
            if tool_type == 'general_tool':
                self.general_tools[idx] = entry
            elif tool_type == 'simple_tool':
                self.simple_tools[idx] = entry
            else:
                self.dialog_tools[idx] = entry
            return

        # toolbar[t][i]=...
        m = re.match(r'toolbar\[(\d+)\]\[(\d+)\]=(.*)$', content)
        if m:
            t = int(m.group(1))
            slot = int(m.group(2))
            fields = m.group(3).split(',')
            ref      = fields[0] if len(fields) > 0 else ''
            icon_ov  = fields[1] if len(fields) > 1 else ''
            key_ov   = fields[2] if len(fields) > 2 else ''
            param    = fields[3] if len(fields) > 3 else ''
            helpfile = fields[4] if len(fields) > 4 else ''
            entry = ToolbarEntry(t, slot, ref, icon_ov, key_ov, param, helpfile, comment)
            if t not in self.toolbars:
                self.toolbars[t] = {}
            self.toolbars[t][slot] = entry
            return

    def save(self, path=None):
        """Save back, updating changed lines."""
        if path is None:
            path = self.filepath
        # Rebuild from scratch using original lines but replacing known entries
        result = []
        written_general = set()
        written_simple  = set()
        written_dialog  = set()
        written_toolbar = set()  # (t, slot)

        for raw_line in self.lines:
            line = raw_line.rstrip('\n').rstrip('\r')
            content = line.strip()

            if not content or content.startswith('#'):
                result.append(raw_line if raw_line.endswith('\n') else raw_line + '\n')
                continue

            # Strip comment for matching
            bare = content
            if ' #' in bare:
                bare = bare[:bare.index(' #')].strip()

            m = re.match(r'(general_tool|simple_tool|dialog_tool)\[(\d+)\]', bare)
            if m:
                tool_type = m.group(1)
                idx = int(m.group(2))
                if tool_type == 'general_tool' and idx in self.general_tools:
                    result.append(self.general_tools[idx].to_string() + '\n')
                    written_general.add(idx)
                elif tool_type == 'simple_tool' and idx in self.simple_tools:
                    result.append(self.simple_tools[idx].to_string() + '\n')
                    written_simple.add(idx)
                elif tool_type == 'dialog_tool' and idx in self.dialog_tools:
                    result.append(self.dialog_tools[idx].to_string() + '\n')
                    written_dialog.add(idx)
                else:
                    result.append(raw_line if raw_line.endswith('\n') else raw_line + '\n')
                continue

            m = re.match(r'toolbar\[(\d+)\]\[(\d+)\]', bare)
            if m:
                t = int(m.group(1)); slot = int(m.group(2))
                key = (t, slot)
                if t in self.toolbars and slot in self.toolbars[t]:
                    result.append(self.toolbars[t][slot].to_string() + '\n')
                    written_toolbar.add(key)
                else:
                    result.append(raw_line if raw_line.endswith('\n') else raw_line + '\n')
                continue

            result.append(raw_line if raw_line.endswith('\n') else raw_line + '\n')

        with open(path, 'w', encoding='utf-8') as f:
            f.writelines(result)

    def toolbar_count(self):
        return len(self.toolbars)

    def get_toolbar_name(self, toolbar_id):
        """Return a descriptive name for a toolbar based on its first entry."""
        if toolbar_id == 0:
            return 'Main Menu'
        tb = self.toolbars.get(toolbar_id, {})
        # Look for toolbar entries that reference this toolbar to get its name
        for t in self.toolbars.values():
            for slot in t.values():
                if slot.ref == f'toolbar[{toolbar_id}]':
                    if slot.param:
                        return f'[{toolbar_id}] {slot.param}'
                    if slot.helpfile:
                        return f'[{toolbar_id}] {slot.helpfile}'
        # Fallback: show ref of first slot
        first = tb.get(0)
        if first:
            return f'toolbar[{toolbar_id}]'
        return f'toolbar[{toolbar_id}]'


# ─────────────────────────────────────────────────────────────────────────────
# MAIN GUI
# ─────────────────────────────────────────────────────────────────────────────

ICON_SIZE    = 36   # display size in grid
ICON_PADDING = 2
GRID_COLS    = 8

TOOL_TYPE_LABELS = {
    'general_tool': 'GT',
    'simple_tool':  'ST',
    'dialog_tool':  'DT',
    'toolbar':      'TB',
}

TOOL_TYPE_OPTIONS = [
    'general_tool', 'simple_tool', 'dialog_tool',
    'toolbar', 'ways', 'signs', 'wayobjs', 'buildings',
    'bridges', 'tunnels', 'scripts', 'depots', 'stops',
]


class ToolIconButton(tk.Frame):
    """A single clickable icon cell in the toolbar grid."""

    def __init__(self, parent, entry: ToolbarEntry, photo_img, label, on_click, **kw):
        super().__init__(parent, **kw)
        self.entry    = entry
        self.selected = False

        size = ICON_SIZE
        self.canvas = tk.Canvas(self, width=size, height=size,
                                bd=1, relief='flat', cursor='hand2',
                                highlightthickness=1, highlightbackground='#888')
        self.canvas.pack()

        if photo_img:
            self.canvas.create_image(size//2, size//2, image=photo_img, anchor='center')
            self._img_ref = photo_img  # keep reference
        else:
            # Placeholder: colored rectangle with label
            self.canvas.create_rectangle(2, 2, size-2, size-2, fill='#5a7a5a', outline='')
            self.canvas.create_text(size//2, size//2, text=label, fill='white',
                                    font=('Helvetica', 7))

        self.canvas.bind('<Button-1>', lambda e: on_click(self))
        self.bind('<Button-1>', lambda e: on_click(self))

    def set_selected(self, selected):
        self.selected = selected
        color = '#3080ff' if selected else '#888'
        self.canvas.configure(highlightbackground=color,
                              highlightthickness=2 if selected else 1)


class MainApp(tk.Tk):

    def __init__(self, initial_file=None):
        super().__init__()
        self.title('Simutrans menuconf.tab Editor')
        self.geometry('1100x700')
        self.minsize(800, 500)

        self.menuconf  = MenuconfFile()
        self.pak_cache = PakImageCache()
        self.pak_dir   = None
        self.current_toolbar_id = 0
        self.selected_slot_widget = None
        self.selected_entry: ToolbarEntry = None
        self._icon_buttons = []  # current grid buttons

        self._build_menu()
        self._build_ui()

        if initial_file and os.path.exists(initial_file):
            self._load_file(initial_file)

    # ── Menu bar ──────────────────────────────────────────────────────────────

    def _build_menu(self):
        mb = tk.Menu(self)
        self.config(menu=mb)

        fm = tk.Menu(mb, tearoff=0)
        fm.add_command(label='Open menuconf.tab…', command=self._open_file, accelerator='Ctrl+O')
        fm.add_command(label='Save',                command=self._save,      accelerator='Ctrl+S')
        fm.add_command(label='Save As…',            command=self._save_as)
        fm.add_separator()
        fm.add_command(label='Quit',                command=self.quit)
        mb.add_cascade(label='File', menu=fm)

        pm = tk.Menu(mb, tearoff=0)
        pm.add_command(label='Select pak directory…', command=self._open_pak_dir)
        mb.add_cascade(label='Pak', menu=pm)

        self.bind('<Control-o>', lambda e: self._open_file())
        self.bind('<Control-s>', lambda e: self._save())

    # ── Main layout ───────────────────────────────────────────────────────────

    def _build_ui(self):
        # Top status bar
        self.status_var = tk.StringVar(value='Open a menuconf.tab to start.')
        status = tk.Label(self, textvariable=self.status_var, anchor='w',
                          relief='sunken', bd=1, font=('Helvetica', 9))
        status.pack(side='bottom', fill='x')

        # Main paned window
        paned = tk.PanedWindow(self, orient='horizontal', sashwidth=5,
                               sashrelief='raised')
        paned.pack(fill='both', expand=True)

        # ── Left: toolbar tree ──
        left = tk.Frame(paned, width=220)
        paned.add(left, minsize=160)
        tk.Label(left, text='Toolbars', font=('Helvetica', 10, 'bold')).pack(anchor='w', padx=4)
        self.toolbar_tree = ttk.Treeview(left, show='tree', selectmode='browse')
        sb = ttk.Scrollbar(left, orient='vertical', command=self.toolbar_tree.yview)
        self.toolbar_tree.configure(yscrollcommand=sb.set)
        self.toolbar_tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.toolbar_tree.bind('<<TreeviewSelect>>', self._on_toolbar_select)

        # ── Right: icon grid + editor ──
        right_paned = tk.PanedWindow(paned, orient='vertical', sashwidth=5, sashrelief='raised')
        paned.add(right_paned, minsize=400)

        # Grid area
        grid_frame = tk.Frame(right_paned)
        right_paned.add(grid_frame, minsize=150)
        tk.Label(grid_frame, text='Tools in toolbar', font=('Helvetica', 10, 'bold')).pack(anchor='w', padx=4)
        self.grid_canvas = tk.Canvas(grid_frame, bg='#2b2b2b')
        gsb_y = ttk.Scrollbar(grid_frame, orient='vertical',   command=self.grid_canvas.yview)
        gsb_x = ttk.Scrollbar(grid_frame, orient='horizontal', command=self.grid_canvas.xview)
        self.grid_canvas.configure(yscrollcommand=gsb_y.set, xscrollcommand=gsb_x.set)
        gsb_x.pack(side='bottom', fill='x')
        gsb_y.pack(side='right',  fill='y')
        self.grid_canvas.pack(side='left', fill='both', expand=True)
        self.grid_inner = tk.Frame(self.grid_canvas, bg='#2b2b2b')
        self.grid_canvas.create_window((0, 0), window=self.grid_inner, anchor='nw')
        self.grid_inner.bind('<Configure>', lambda e: self.grid_canvas.configure(
            scrollregion=self.grid_canvas.bbox('all')))

        # Editor area
        editor_frame = tk.LabelFrame(right_paned, text='Tool Editor', padx=6, pady=6)
        right_paned.add(editor_frame, minsize=180)
        self._build_editor(editor_frame)

    def _build_editor(self, parent):
        """Build the entry editor panel."""
        # Row 0: slot info
        info_row = tk.Frame(parent); info_row.pack(fill='x', pady=2)
        tk.Label(info_row, text='Slot:').pack(side='left')
        self.lbl_slot = tk.Label(info_row, text='-', width=5, relief='sunken')
        self.lbl_slot.pack(side='left', padx=4)
        tk.Label(info_row, text='Ref:').pack(side='left', padx=(8,0))
        self.var_ref = tk.StringVar()
        self.ent_ref = tk.Entry(info_row, textvariable=self.var_ref, width=28)
        self.ent_ref.pack(side='left', padx=2)

        # Row 1: icon / key / parameter
        row1 = tk.Frame(parent); row1.pack(fill='x', pady=2)
        tk.Label(row1, text='Icon override:').pack(side='left')
        self.var_icon = tk.StringVar()
        tk.Entry(row1, textvariable=self.var_icon, width=6).pack(side='left', padx=2)
        tk.Label(row1, text='Key override:').pack(side='left', padx=(8,0))
        self.var_key = tk.StringVar()
        tk.Entry(row1, textvariable=self.var_key, width=8).pack(side='left', padx=2)
        tk.Label(row1, text='Parameter:').pack(side='left', padx=(8,0))
        self.var_param = tk.StringVar()
        tk.Entry(row1, textvariable=self.var_param, width=12).pack(side='left', padx=2)
        tk.Label(row1, text='Helpfile:').pack(side='left', padx=(8,0))
        self.var_helpfile = tk.StringVar()
        tk.Entry(row1, textvariable=self.var_helpfile, width=16).pack(side='left', padx=2)

        # Row 2: inline comment
        row2 = tk.Frame(parent); row2.pack(fill='x', pady=2)
        tk.Label(row2, text='Comment:').pack(side='left')
        self.var_comment = tk.StringVar()
        tk.Entry(row2, textvariable=self.var_comment, width=50).pack(side='left', padx=2)

        # Row 3: buttons
        btn_row = tk.Frame(parent); btn_row.pack(fill='x', pady=4)
        tk.Button(btn_row, text='Apply changes', command=self._apply_entry,
                  bg='#3a8a3a', fg='white', font=('Helvetica', 9, 'bold')).pack(side='left', padx=4)
        tk.Button(btn_row, text='Add slot after', command=self._add_slot_after).pack(side='left', padx=4)
        tk.Button(btn_row, text='Delete slot',    command=self._delete_slot,
                  fg='red').pack(side='left', padx=4)
        tk.Button(btn_row, text='Move up',   command=self._move_up).pack(side='left', padx=4)
        tk.Button(btn_row, text='Move down', command=self._move_down).pack(side='left', padx=4)

        # Row 4: preview icon
        prev_row = tk.Frame(parent); prev_row.pack(anchor='w', pady=2)
        tk.Label(prev_row, text='Icon preview:').pack(side='left')
        self.preview_canvas = tk.Canvas(prev_row, width=64, height=64,
                                        bg='#3a3a3a', bd=2, relief='sunken')
        self.preview_canvas.pack(side='left', padx=4)
        self._preview_img_ref = None

    # ── File operations ───────────────────────────────────────────────────────

    def _open_file(self):
        path = filedialog.askopenfilename(
            title='Open menuconf.tab',
            filetypes=[('Tab files', '*.tab'), ('All files', '*.*')])
        if path:
            self._load_file(path)

    def _load_file(self, path):
        try:
            self.menuconf.load(path)
        except Exception as e:
            messagebox.showerror('Error', f'Failed to load:\n{e}')
            return
        self.title(f'menuconf editor – {os.path.basename(path)}')
        self.status_var.set(f'Loaded: {path}')
        self._rebuild_toolbar_tree()
        # Pak files are always one level up from menuconf.tab (i.e. config/../)
        pak_dir = os.path.dirname(os.path.dirname(path))
        self._load_pak_dir(pak_dir)
        self._refresh_current_toolbar()

    def _save(self):
        if not self.menuconf.filepath:
            self._save_as(); return
        try:
            self.menuconf.save()
            self.status_var.set(f'Saved: {self.menuconf.filepath}')
        except Exception as e:
            messagebox.showerror('Error', f'Save failed:\n{e}')

    def _save_as(self):
        path = filedialog.asksaveasfilename(
            title='Save menuconf.tab as',
            defaultextension='.tab',
            filetypes=[('Tab files', '*.tab'), ('All files', '*.*')])
        if path:
            try:
                self.menuconf.save(path)
                self.menuconf.filepath = path
                self.status_var.set(f'Saved: {path}')
            except Exception as e:
                messagebox.showerror('Error', f'Save failed:\n{e}')

    def _open_pak_dir(self):
        d = filedialog.askdirectory(title='Select pak directory (containing menu.*.pak files)')
        if d:
            self._load_pak_dir(d)
            self._refresh_current_toolbar()

    def _load_pak_dir(self, d):
        self.pak_dir = d
        self.pak_cache.load_pak_dir(d, self.menuconf.icon_width or 32)
        pak_count = len(self.pak_cache.pak_files)
        self.status_var.set(f'Pak dir: {d}  ({pak_count} pak files loaded)')

    # ── Toolbar tree ──────────────────────────────────────────────────────────

    def _rebuild_toolbar_tree(self):
        self.toolbar_tree.delete(*self.toolbar_tree.get_children())
        for tb_id in sorted(self.menuconf.toolbars.keys()):
            name = self.menuconf.get_toolbar_name(tb_id)
            slots = len(self.menuconf.toolbars[tb_id])
            label = f'[{tb_id}] {name} ({slots})'
            self.toolbar_tree.insert('', 'end', iid=str(tb_id), text=label)
        # Select toolbar 0
        if '0' in self.toolbar_tree.get_children():
            self.toolbar_tree.selection_set('0')
            self._show_toolbar(0)

    def _on_toolbar_select(self, event):
        sel = self.toolbar_tree.selection()
        if not sel:
            return
        self._show_toolbar(int(sel[0]))

    def _show_toolbar(self, toolbar_id):
        self.current_toolbar_id = toolbar_id
        # Clear grid
        for w in self.grid_inner.winfo_children():
            w.destroy()
        self._icon_buttons.clear()
        self.selected_slot_widget = None
        self.selected_entry = None

        tb = self.menuconf.toolbars.get(toolbar_id, {})
        for col_i, slot in enumerate(sorted(tb.keys())):
            entry = tb[slot]
            photo = self._get_entry_icon(entry)
            label = self._short_label(entry)
            row_i = col_i // GRID_COLS
            col_j = col_i % GRID_COLS

            cell = tk.Frame(self.grid_inner, bg='#2b2b2b')
            cell.grid(row=row_i*2, column=col_j, padx=ICON_PADDING, pady=ICON_PADDING)

            btn = ToolIconButton(cell, entry, photo, label,
                                 on_click=self._on_icon_click,
                                 bg='#2b2b2b')
            btn.pack()
            # Slot number label below icon
            tk.Label(cell, text=str(slot), font=('Helvetica', 7),
                     fg='#aaa', bg='#2b2b2b').pack()

            self._icon_buttons.append(btn)

        # "Add new slot" button
        col_i = len(tb)
        row_i = col_i // GRID_COLS
        col_j = col_i % GRID_COLS
        add_btn = tk.Button(self.grid_inner, text='+', width=3, height=2,
                            bg='#444', fg='white', relief='flat',
                            command=self._add_new_slot)
        add_btn.grid(row=row_i*2, column=col_j, padx=ICON_PADDING, pady=ICON_PADDING,
                     sticky='ns')

    def _refresh_current_toolbar(self):
        self._show_toolbar(self.current_toolbar_id)

    def _get_entry_icon(self, entry: ToolbarEntry):
        """Try to resolve the icon for a toolbar entry."""
        # If entry has an icon override, use BarTools pak
        if entry.icon:
            img = self.pak_cache.get_tk_image('menu.BarTools', entry.icon_num)
            if img:
                return img

        # Resolve from ref
        ref_type = entry.ref_type
        ref_idx  = entry.ref_index

        if ref_type == 'general_tool':
            te = self.menuconf.general_tools.get(ref_idx)
            if te and te.icon:
                return self.pak_cache.get_tk_image('menu.GeneralTools', te.icon_num)
        elif ref_type == 'simple_tool':
            te = self.menuconf.simple_tools.get(ref_idx)
            if te and te.icon:
                return self.pak_cache.get_tk_image('menu.SimpleTools', te.icon_num)
        elif ref_type == 'dialog_tool':
            te = self.menuconf.dialog_tools.get(ref_idx)
            if te and te.icon:
                return self.pak_cache.get_tk_image('menu.DialogeTools', te.icon_num)
        elif ref_type == 'toolbar':
            # toolbar reference: use toolbar pak with the icon override
            pass
        return None

    def _short_label(self, entry: ToolbarEntry):
        rt = entry.ref_type
        ri = entry.ref_index
        prefix = TOOL_TYPE_LABELS.get(rt, rt[:2].upper())
        if ri >= 0:
            return f'{prefix}{ri}'
        return entry.ref[:5]

    # ── Icon grid events ──────────────────────────────────────────────────────

    def _on_icon_click(self, btn: ToolIconButton):
        if self.selected_slot_widget:
            self.selected_slot_widget.set_selected(False)
        btn.set_selected(True)
        self.selected_slot_widget = btn
        self.selected_entry = btn.entry
        self._populate_editor(btn.entry)

    def _populate_editor(self, entry: ToolbarEntry):
        self.lbl_slot.config(text=str(entry.slot))
        self.var_ref.set(entry.ref)
        self.var_icon.set(entry.icon)
        self.var_key.set(entry.key)
        self.var_param.set(entry.param)
        self.var_helpfile.set(entry.helpfile)
        self.var_comment.set(entry.comment)
        self._update_preview(entry)

    def _update_preview(self, entry: ToolbarEntry):
        img = self._get_entry_icon(entry)
        self.preview_canvas.delete('all')
        if img:
            self.preview_canvas.create_image(32, 32, image=img, anchor='center')
            self._preview_img_ref = img
        else:
            self.preview_canvas.create_rectangle(4, 4, 60, 60, fill='#555', outline='')
            self.preview_canvas.create_text(32, 32, text='No\nIcon',
                                            fill='#aaa', font=('Helvetica', 9))

    # ── Editor actions ────────────────────────────────────────────────────────

    def _apply_entry(self):
        if self.selected_entry is None:
            return
        entry = self.selected_entry
        entry.ref      = self.var_ref.get()
        entry.icon     = self.var_icon.get()
        entry.key      = self.var_key.get()
        entry.param    = self.var_param.get()
        entry.helpfile = self.var_helpfile.get()
        entry.comment  = self.var_comment.get()
        self.status_var.set(f'Updated toolbar[{entry.toolbar_id}][{entry.slot}]')
        self._refresh_current_toolbar()

    def _add_slot_after(self):
        """Add a new empty slot after the selected slot."""
        tb = self.menuconf.toolbars.setdefault(self.current_toolbar_id, {})
        if self.selected_entry:
            new_slot = self.selected_entry.slot + 1
        else:
            new_slot = (max(tb.keys()) + 1) if tb else 0
        # Shift all slots >= new_slot up by 1
        new_tb = {}
        for s, e in tb.items():
            if s >= new_slot:
                e.slot = s + 1
                new_tb[s+1] = e
            else:
                new_tb[s] = e
        new_entry = ToolbarEntry(self.current_toolbar_id, new_slot, '-')
        new_tb[new_slot] = new_entry
        self.menuconf.toolbars[self.current_toolbar_id] = new_tb
        self._refresh_current_toolbar()

    def _add_new_slot(self):
        tb = self.menuconf.toolbars.setdefault(self.current_toolbar_id, {})
        new_slot = (max(tb.keys()) + 1) if tb else 0
        new_entry = ToolbarEntry(self.current_toolbar_id, new_slot, '-')
        tb[new_slot] = new_entry
        self._refresh_current_toolbar()

    def _delete_slot(self):
        if self.selected_entry is None:
            return
        tb = self.menuconf.toolbars.get(self.current_toolbar_id, {})
        slot = self.selected_entry.slot
        if slot not in tb:
            return
        if not messagebox.askyesno('Delete', f'Delete toolbar[{self.current_toolbar_id}][{slot}]?'):
            return
        del tb[slot]
        # Renumber remaining slots
        new_tb = {i: e for i, (s, e) in enumerate(sorted(tb.items()))}
        for i, e in new_tb.items():
            e.slot = i
        self.menuconf.toolbars[self.current_toolbar_id] = new_tb
        self.selected_entry = None
        self._refresh_current_toolbar()

    def _move_up(self):
        self._move_slot(-1)

    def _move_down(self):
        self._move_slot(+1)

    def _move_slot(self, direction):
        if self.selected_entry is None:
            return
        tb = self.menuconf.toolbars.get(self.current_toolbar_id, {})
        slot = self.selected_entry.slot
        target = slot + direction
        if target < 0 or target not in tb:
            return
        # Swap
        tb[slot].slot, tb[target].slot = target, slot
        tb[slot], tb[target] = tb[target], tb[slot]
        self.selected_entry = tb[target]
        self._refresh_current_toolbar()


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import sys
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    app = MainApp(initial_file=initial)
    app.mainloop()
