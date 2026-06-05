#!/usr/bin/env python3
"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import json
import platform
import os
import glob
import shutil
from datetime import datetime

from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, config_realtime_process
from openpilot.sunnypilot.mapd.proc_sched import config_background_process
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.selfdrived.alertmanager import set_offroad_alert
from openpilot.sunnypilot.mapd.live_map_data.osm_map_data import OsmMapData
from openpilot.system.hardware.hw import Paths
from openpilot.sunnypilot.mapd import MAPD_PATH
from openpilot.sunnypilot.mapd.mapd_installer import VERSION, update_installed_version

# PFEIFER - MAPD {{
params = Params()
mem_params = Params("/dev/shm/params") if platform.system() != "Darwin" else params
# }} PFEIFER - MAPD


def get_files_for_cleanup() -> list[str]:
  paths = [
    f"{Paths.mapd_root()}/db",
    f"{Paths.mapd_root()}/v*"
  ]
  files_to_remove = []
  for path in paths:
    if os.path.exists(path):
      files = glob.glob(path + '/**', recursive=True)
      files_to_remove.extend(files)
  # check for version and mapd files
  if not os.path.isfile(MAPD_PATH):
    files_to_remove.append(MAPD_PATH)
  return files_to_remove


def cleanup_old_osm_data(files_to_remove: list[str]) -> None:
  for file in files_to_remove:
    # Remove trailing slash if path is file
    if file.endswith('/') and os.path.isfile(file[:-1]):
      file = file[:-1]
    # Try to remove as file or symbolic link first
    if os.path.islink(file) or os.path.isfile(file):
      os.remove(file)
    elif os.path.isdir(file):  # If it's a directory
      shutil.rmtree(file, ignore_errors=False)


def request_refresh_osm_location_data(nations: list[str], states: list[str] | None = None) -> None:
  params.put("OsmDownloadedDate", str(datetime.now().timestamp()))
  params.put_bool("OsmDbUpdatesCheck", False)

  osm_download_locations = {
    "nations": nations,
    "states": states or []
  }

  print(f"Downloading maps for {json.dumps(osm_download_locations)}")
  mem_params.put("OSMDownloadLocations", osm_download_locations)


def filter_nations_and_states(nations: list[str], states: list[str] | None = None) -> tuple[list[str], list[str]]:
  """Filters and prepares nation and state data for OSM map download.

  If the nation is 'US' and a specific state is provided, the nation 'US' is removed from the list.
  If the nation is 'US' and the state is 'All', the 'All' is removed from the list.
  The idea behind these filters is that if a specific state in the US is provided,
  there's no need to download map data for the entire US. Conversely,
  if the state is unspecified (i.e., 'All'), we intend to download map data for the whole US,
  and 'All' isn't a valid state name, so it's removed.

  Parameters:
  nations (list): A list of nations for which the map data is to be downloaded.
  states (list, optional): A list of states for which the map data is to be downloaded. Defaults to None.

  Returns:
  tuple: Two lists. The first list is filtered nations and the second list is filtered states.
  """

  if "US" in nations and states and not any(x.lower() == "all" for x in states):
    # If a specific state in the US is provided, remove 'US' from nations
    nations.remove("US")
  elif "US" in nations and states and any(x.lower() == "all" for x in states):
    # If 'All' is provided as a state (case invariant), remove those instances from states
    states = [x for x in states if x.lower() != "all"]
  elif "US" not in nations and states and any(x.lower() == "all" for x in states):
    states.remove("All")
  return nations, states or []


# --- OSM working-set bounding -------------------------------------------------------
# ROOT CAUSE (proven from on-device diag logs): the map matcher mmaps the downloaded
# OSM DB. The legacy default OsmStateName="All" resolves (via filter_nations_and_states)
# to the nation "US", so mapd loads the ENTIRE United States road graph -- multi-GB.
# On a device with ~31 MB free + 1.5 GB page cache, every map-match query evicts pages,
# kswapd churns at ~30% CPU, and EVERY service (liveTorqueParameters, driverMonitoring,
# liveParameters, modelDataV2SP ...) intermittently misses its deadline -> selfdrived
# raises commIssue -> "take control immediately" -> no engage. mapd's own RSS is only
# ~140 MB; the damage is page-cache thrash from the oversized mmap, not a leak.
#
# FIX: keep OSMDownloadBounds tracking a ~80 km box around the car so the resident
# working set stays small and always covers the road ahead. Hysteresis (half-radius)
# avoids rewriting every tick.
OSM_BOUND_RADIUS_DEG = float(os.environ.get("OSM_BOUND_RADIUS_DEG", "0.75"))  # ~83 km


def _osm_bounds_for(lat: float, lon: float, r: float = OSM_BOUND_RADIUS_DEG) -> dict:
  return {"min_lat": round(lat - r, 4), "min_lon": round(lon - r, 4),
          "max_lat": round(lat + r, 4), "max_lon": round(lon + r, 4)}


def update_osm_bounds() -> None:
  """Keep OSMDownloadBounds following the car so mapd never holds a whole nation."""
  try:
    pos = json.loads(mem_params.get("LastGPSPosition") or "{}")
    lat, lon = pos.get("latitude"), pos.get("longitude")
    if lat is None or lon is None:
      return
    cur_raw = mem_params.get("OSMDownloadBounds")
    cur = json.loads(cur_raw) if cur_raw else None
    if cur:
      cy = (cur["min_lat"] + cur["max_lat"]) / 2.0
      cx = (cur["min_lon"] + cur["max_lon"]) / 2.0
      if abs(lat - cy) < OSM_BOUND_RADIUS_DEG / 2 and abs(lon - cx) < OSM_BOUND_RADIUS_DEG / 2:
        return  # still well inside the current box -> no churn
    bounds = _osm_bounds_for(lat, lon)
    mem_params.put("OSMDownloadBounds", json.dumps(bounds))
    cloudlog.warning(f"mapd: OSM working-set bounded to {bounds}")
  except Exception:
    cloudlog.exception("mapd: update_osm_bounds failed")


def update_osm_db() -> None:
  if params.get_bool("OsmDbUpdatesCheck"):
    cleanup_old_osm_data(get_files_for_cleanup())
    country = params.get("OsmLocationName", return_default=True)
    state = params.get("OsmStateName", return_default=True)
    filtered_nations, filtered_states = filter_nations_and_states([country], [state])
    request_refresh_osm_location_data(filtered_nations, filtered_states)

  update_osm_bounds()   # bound the working set to a box around the car (anti-thrash)

  if not mem_params.get("LastGPSPosition"):
    mem_params.put("LastGPSPosition", "{}")


def main_thread():
  update_installed_version(VERSION, params)
  config_background_process([0, 1, 2, 3])  # was SCHED_FIFO prio5 -> starved monitoring; now niced

  rk = Ratekeeper(1, print_delay_threshold=None)
  live_map_sp = OsmMapData()

  # Create folder needed for OSM
  try:
    os.mkdir(Paths.mapd_root())
  except FileExistsError:
    pass
  except PermissionError:
    cloudlog.exception(f"mapd: failed to make {Paths.mapd_root()}")

  while True:
    show_alert = get_files_for_cleanup() and params.get_bool("OsmLocal")
    set_offroad_alert("Offroad_OSMUpdateRequired", show_alert, "This alert will be cleared when new maps are downloaded.")

    update_osm_db()
    live_map_sp.tick()
    rk.keep_time()


def main():
  main_thread()


if __name__ == "__main__":
  main()
