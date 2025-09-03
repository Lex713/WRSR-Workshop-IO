bl_info = {
    "name": "Workers & Resources Steam Workshop IO",
    "author": "Lex713",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > WRSR",
    "description": "Generate workshopconfig.ini files and upload/update items to Steam Workshop for Workers & Resources: Soviet Republic",
    "category": "Import-Export",
}

import bpy
import os
import sys
import threading
import subprocess
import time
import re
import json
from pathlib import Path
from bpy.props import (
    StringProperty, BoolProperty, IntProperty, EnumProperty, CollectionProperty, FloatProperty
)
from bpy.types import PropertyGroup, Operator, Panel, AddonPreferences, UIList



# -----------------------------
# Helpers
# -----------------------------

def _addon_dir() -> Path:
    # this file: .../wrsr_workshop_io/__init__.py
    return Path(__file__).resolve().parent

def bundled_steamcmd_path() -> Path:
    scmd = _addon_dir() / "steamcmd" / "steamcmd.exe"
    return scmd

def steamid64_txt_path() -> Path:
    return _addon_dir() / "steamid64.txt"

def write_steamid64_to_file(steamid64: str):
    p = steamid64_txt_path()
    try:
        p.write_text(steamid64.strip() + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[wrsr_workshop_io] Failed to write steamid64.txt: {e}")

def read_steamid64_from_file() -> str:
    p = steamid64_txt_path()
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""

def _default_steam_paths() -> list[Path]:
    # Used by DetectSteamID operator; very Windows-centric for now.
    candidates = []
    # Steam default locations
    candidates.append(Path(os.getenv("PROGRAMFILES(X86)", r"C:\\Program Files (x86)")) / "Steam" / "config" / "loginusers.vdf")
    candidates.append(Path(os.getenv("PROGRAMFILES", r"C:\\Program Files")) / "Steam" / "config" / "loginusers.vdf")
    # Common alternative libraries
    candidates.append(Path(os.getenv("STEAM_CONFIG", "")))
    # Drop empties
    return [p for p in candidates if p and str(p) != ""]

# -----------------------------
# Background worker
# -----------------------------

class SteamCmdWorker(threading.Thread):
    """Run steamcmd in a background thread and capture a few lines of output."""

    def __init__(self, username: str, password: str, cmd_args, out_buffer, done_flag, prefs_ref):
        super().__init__(daemon=True)
        self.username = username
        self.password = password
        self.cmd_args = cmd_args
        self.out_buffer = out_buffer
        self.done_flag = done_flag
        self.prefs_ref = prefs_ref

    def run(self):
        scmd = bundled_steamcmd_path()
        if not scmd.exists():
            self.out_buffer.append(f"steamcmd not found: {scmd}")
            self.done_flag["ok"] = False
            return

        # Compose command; we keep it minimal and quit ASAP.
        # NOTE: If Steam Guard is enabled, SteamCMD may prompt for a code in stdout.
        # This scaffold does not yet handle interactive code entry in-UI; we will add that next.
        cmd = [
            str(scmd),
            "+login", self.username, self.password,
            "+quit",
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(scmd.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                universal_newlines=True,
                bufsize=1,
                # creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith("win") else 0,
            )
        except Exception as e:
            self.out_buffer.append(f"Failed to start steamcmd: {e}")
            self.done_flag["ok"] = False
            return

        # Read a limited tail of output to avoid huge logs in prefs
        tail_limit = 50
        login_ok = False
        steamid_candidate = None

        for line in proc.stdout:  # type: ignore[arg-type]
            line = line.rstrip()
            # keep a rolling buffer
            self.out_buffer.append(line)
            if len(self.out_buffer) > tail_limit:
                del self.out_buffer[0]

            # Heuristics
            if "Logged in OK" in line or "Waiting for user info...OK" in line:
                login_ok = True
            # Sometimes SteamCMD prints an account line we can parse
            # Try to catch something that looks like a 17-digit number
            m = re.search(r"\b(76\d{15})\b", line)
            if m:
                steamid_candidate = m.group(1)

        proc.wait()

        self.done_flag["ok"] = login_ok and (proc.returncode == 0)

        # Try to persist steamid64 if we scraped it, otherwise keep whatever is in prefs
        if steamid_candidate:
            write_steamid64_to_file(steamid_candidate)
            self.prefs_ref.steamid64 = steamid_candidate


class SteamCmdBuildWorker(threading.Thread):
    def __init__(self, cmd_args, out_buffer, done_flag):
        super().__init__(daemon=True)
        self.cmd_args = cmd_args
        self.out_buffer = out_buffer
        self.done_flag = done_flag

    def run(self):
        scmd = bundled_steamcmd_path()
        if not scmd.exists():
            self.out_buffer.append(f"steamcmd not found: {scmd}")
            self.done_flag["ok"] = False
            return

        try:
            proc = subprocess.Popen(
                self.cmd_args,
                cwd=str(scmd.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
        except Exception as e:
            self.out_buffer.append(f"Failed to start steamcmd: {e}")
            self.done_flag["ok"] = False
            return

        tail_limit = 50
        for line in proc.stdout:
            line = line.rstrip()
            self.out_buffer.append(line)
            if len(self.out_buffer) > tail_limit:
                del self.out_buffer[0]

        proc.wait()
        self.done_flag["ok"] = (proc.returncode == 0)


# -----------------------------
# Preferences UI
# -----------------------------

class WRSR_WorkshopPrefs(bpy.types.AddonPreferences):
    bl_idname = __name__

    steam_username: bpy.props.StringProperty(name="Steam Login", default="")
    steam_password: bpy.props.StringProperty(name="Steam Password", subtype="PASSWORD", default="")
    steamid64: bpy.props.StringProperty(name="SteamID64", default="", description="17-digit SteamID64 (auto-detected if possible)")

    # Runtime-only state (not saved)
    _status_lines: list[str] = []
    _worker_done: dict = {"ok": False, "running": False}

    def draw(self, context):
        layout = self.layout
        col = layout.column()
        col.label(text="Steam Login (runs via bundled SteamCMD)")
        col.prop(self, "steam_username")
        col.prop(self, "steam_password")

        row = col.row()
        row.operator("wrsr.login_steamcmd", text="Login", icon="URL")
        row.operator("wrsr.detect_steamid", text="Detect SteamID64", icon="EYEDROPPER")

        col.separator()
        col.prop(self, "steamid64")

        # Show a short status log
        box = col.box()
        box.label(text="SteamCMD Status")
        if self._worker_done.get("running", False):
            box.label(text="Running in background…")
        for ln in self._status_lines[-10:]:
            box.label(text=ln)

        # Show where files live for sanity
        col.separator()
        col.label(text=f"Bundled steamcmd: {bundled_steamcmd_path()}")
        sidp = steamid64_txt_path()
        col.label(text=f"steamid64.txt: {sidp}")
        
    def resolve_steamcmd(self):
        addon_dir = os.path.dirname(__file__)
        steamcmd_exe = os.path.join(addon_dir, "steamcmd", "steamcmd.exe")
        return steamcmd_exe
        
# -----------------------------
# SCENE PROPERTIES (WORKSHOP DATA)
# -----------------------------


def get_workshop_props():
    scene = bpy.context.scene
    if not hasattr(scene, "wrsr_workshop_itemid"):
        scene["wrsr_workshop_itemid"] = ""
    return scene

# -----------------------------
# UI PANEL
# -----------------------------

class WORKSHOP_PT_Main(bpy.types.Panel):
    bl_label = "WR:SR Workshop"
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = 'scene'


    def draw(self, context):
        layout = self.layout
        scene = context.scene


        layout.prop(scene, "wrsr_workshop_itemid", text="Workshop Item ID")
        layout.operator("wrsr.create_new_item", text="Create New Workshop Item", icon="PLUS")


        box = layout.box()
        box.label(text="SteamCMD Status")
        prefs = context.preferences.addons[__name__].preferences
        if prefs._worker_done.get("running", False):
            box.label(text="Running in background…")
        for ln in prefs._status_lines[-6:]:
            box.label(text=ln)


# -----------------------------
# Operators
# -----------------------------

class WRSR_OT_LoginSteamCmd(bpy.types.Operator):
    bl_idname = "wrsr.login_steamcmd"
    bl_label = "Login via SteamCMD"
    bl_description = "Log into Steam via bundled SteamCMD in a background thread"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        # Reset status
        prefs._status_lines.clear()
        prefs._worker_done = {"ok": False, "running": True}
        
        login_args = [
            str(bundled_steamcmd_path()),
            "+login", prefs.steam_username, prefs.steam_password,
            "+quit"
        ]

        # Kick background thread
        worker = SteamCmdWorker(
            username=prefs.steam_username,
            password=prefs.steam_password,
            cmd_args=login_args,
            out_buffer=prefs._status_lines,
            done_flag=prefs._worker_done,
            prefs_ref=prefs,
        )
        worker.start()

        # Start a timer to check when it finishes, then write steamid64 if we already have one
        def _check_done():
            if prefs._worker_done.get("running") and not worker.is_alive():
                prefs._worker_done["running"] = False
                ok = prefs._worker_done.get("ok", False)
                if ok:
                    # If not already set, try to keep any prior file value
                    if not prefs.steamid64:
                        existing = read_steamid64_from_file()
                        if existing:
                            prefs.steamid64 = existing
                else:
                    prefs._status_lines.append("Login did not complete successfully.")
                # Force UI redraw
                for window in context.window_manager.windows:
                    for area in window.screen.areas:
                        if area.type == 'PREFERENCES':
                            area.tag_redraw()
                return None  # stop timer
            return 0.5  # keep polling

        bpy.app.timers.register(_check_done, first_interval=0.5)
        return {'FINISHED'}


class WRSR_OT_DetectSteamID(bpy.types.Operator):
    bl_idname = "wrsr.detect_steamid"
    bl_label = "Detect SteamID64"
    bl_description = "Attempt to detect SteamID64 from local Steam loginusers.vdf and save to steamid64.txt"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        candidates = _default_steam_paths()
        found = None
        for vdf in candidates:
            try:
                if vdf and vdf.exists():
                    txt = vdf.read_text(encoding='utf-8', errors='ignore')
                    # Very rough VDF parse: look for 17-digit numbers starting with 76…
                    m = re.findall(r"\b(76\d{15})\b", txt)
                    if m:
                        # Prefer the first; in future we can match by account name
                        found = m[0]
                        break
            except Exception as e:
                print(f"[wrsr_workshop_io] Could not read {vdf}: {e}")

        if found:
            prefs.steamid64 = found
            write_steamid64_to_file(found)
            self.report({'INFO'}, f"Detected SteamID64: {found}")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "Could not detect SteamID64 automatically. Please enter it manually.")
            return {'CANCELLED'}
      
      
class WRSR_OT_CreateNewItem(bpy.types.Operator):
    bl_idname = "wrsr.create_new_item"
    bl_label = "Create New Workshop Item"

    def execute(self, context):
        prefs = context.preferences.addons[__name__].preferences
        prefs._status_lines.clear()
        prefs._worker_done = {"ok": False, "running": True}

        # create a minimal vdf
        vdf_path = _addon_dir() / "workshop.vdf"
        vdf_text = (
            '"workshopitem"\n'
            '{\n'
            '   "appid" "784150"\n'
            '   "contentfolder" ""\n'
            '   "previewfile" ""\n'
            '   "visibility" "2"\n'
            '   "title" "New Workshop Item"\n'
            '   "description" "Created via Blender addon"\n'
            '}\n'
            )
        vdf_path.write_text(vdf_text, encoding="utf-8")

        # Build SteamCMD command
        args = [
            str(bundled_steamcmd_path()),
            "+login", prefs.steam_username, prefs.steam_password,
            "+workshop_build_item", str(vdf_path),
            "+quit"
        ]

        # Start background thread
        worker = SteamCmdBuildWorker(args, prefs._status_lines, prefs._worker_done)
        worker.start()

        def _check():
            if prefs._worker_done.get("running") and not worker.is_alive():
                prefs._worker_done["running"] = False
                if worker.new_item_id:
                    scene.workshop_itemid = worker.new_item_id
                    self.report({'INFO'}, f"Created new Workshop item {worker.new_item_id}")
                else:
                    self.report({'ERROR'}, "Failed to create Workshop item")
                return None
            return 0.5
        bpy.app.timers.register(_check, first_interval=0.5)
        return {'FINISHED'}

# -----------------------------
# Registration
# -----------------------------

classes = (
    WRSR_WorkshopPrefs,
    WRSR_OT_LoginSteamCmd,
    WRSR_OT_DetectSteamID,
    WRSR_OT_CreateNewItem,
    
    WORKSHOP_PT_Main,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    # Sync prefs with file if present
    try:
        addon_prefs = bpy.context.preferences.addons[__name__].preferences
        existing = read_steamid64_from_file()
        if existing:
            addon_prefs.steamid64 = existing
    except Exception:
        pass


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
