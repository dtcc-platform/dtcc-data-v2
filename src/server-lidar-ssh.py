import os
import json
import io
import zipfile

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse

# ----- Added lines for SSH Auth -----
import paramiko
from fastapi import Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

SSH_HOST = "data2.dtcc.chalmers.se"
SSH_PORT = 22  # default SSH port

# We'll do a small function to test SSH auth
def ssh_authenticate(username: str, password: str) -> bool:
    """Try SSH login to data2.dtcc.chalmers.se. Return True if success, else False."""
    ssh_client = paramiko.SSHClient()
    ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh_client.connect(
            hostname=SSH_HOST,
            port=SSH_PORT,
            username=username,
            password=password,
            timeout=5
        )
        ssh_client.close()
        return True
    except paramiko.AuthenticationException:
        return False
    except Exception:
        return False

async def ssh_auth_middleware(request: Request, call_next):
    """
    Middleware that checks X-Username and X-Password headers.
    If SSH auth fails, return 401. Otherwise, proceed.
    """
    # Read headers
    username = request.headers.get("X-Username")
    password = request.headers.get("X-Password")

    if not username or not password:
        # No credentials
        return Response(
            content="Missing X-Username or X-Password header",
            status_code=status.HTTP_401_UNAUTHORIZED
        )

    # Attempt SSH auth
    if not ssh_authenticate(username, password):
        return Response(
            content="SSH authentication failed",
            status_code=status.HTTP_401_UNAUTHORIZED
        )

    # If ok, continue
    response = await call_next(request)
    return response
# ----- End of added lines for SSH Auth -----


# ------------------------------------------------------------------------
# 1. Data Model for the incoming request (all integers)
# ------------------------------------------------------------------------
class LidarRequest(BaseModel):
    xmin: int
    ymin: int
    xmax: int
    ymax: int
    buffer: int = 0  # optional, defaults to 0


# ------------------------------------------------------------------------
# 2. Load the atlas.json (with all integers)
# ------------------------------------------------------------------------
ATLAS_PATH = "atlas.json"
LAZ_DIRECTORY = "."  # Where your actual .laz files reside
ATLAS_PATH = "/mnt/raid0/testingexclude/out/atlas.json"
LAZ_DIRECTORY = "/mnt/raid0/testingexclude/out"  # Where your actual .laz files reside

if not os.path.exists(ATLAS_PATH):
    raise FileNotFoundError(f"Atlas file not found: {ATLAS_PATH}")

with open(ATLAS_PATH, "r") as f:
    atlas_data_raw = json.load(f)

# Convert keys & widths/heights to int
atlas_data = {}
for x_str, y_dict in atlas_data_raw.items():
    x_int = int(x_str)
    if str(x_int) not in atlas_data:
        atlas_data[str(x_int)] = {}
    for y_str, tile_info in y_dict.items():
        y_int = int(y_str)
        w = int(tile_info["width"])
        h = int(tile_info["height"])
        atlas_data[str(x_int)][str(y_int)] = {
            "filename": tile_info["filename"],
            "width": w,
            "height": h
        }

# ------------------------------------------------------------------------
# 3. Utility: check if two bounding boxes intersect (integers)
# ------------------------------------------------------------------------
def bboxes_intersect(axmin, aymin, axmax, aymax,
                     bxmin, bymin, bxmax, bymax):
    """
    Integer-based intersection check for two bounding boxes:
    (axmin, aymin, axmax, aymax) and (bxmin, bymin, bxmax, bymax).
    Returns True if they overlap.
    """
    return not (
        axmax < bxmin or  # A is left of B
        axmin > bxmax or  # A is right of B
        aymax < bymin or  # A is below B
        aymin > bymax     # A is above B
    )


# ------------------------------------------------------------------------
# 4. Create the FastAPI app
# ------------------------------------------------------------------------
app = FastAPI()

# ----- Added line to mount the SSH auth middleware -----
app.add_middleware(BaseHTTPMiddleware, dispatch=ssh_auth_middleware)
# -------------------------------------------------------


@app.post("/get_lidar")
def get_lidar_tiles(req: LidarRequest):
    """
    POST payload example (all integers):
    {
      "xmin": 267000,
      "ymin": 6519000,
      "xmax": 268000,
      "ymax": 6521000,
      "buffer": 100
    }

    1) Expand the requested bounding box by 'buffer' on all sides.
    2) Find all tiles in atlas.json that intersect that expanded box.
    3) Return a JSON with each tile's filename and bounding box in EPSG:3006.
    """

    # 1) Expand bounding box with buffer (all int)
    bxmin = req.xmin - req.buffer
    bymin = req.ymin - req.buffer
    bxmax = req.xmax + req.buffer
    bymax = req.ymax + req.buffer

    # 2) Find intersecting tiles, returning each tile's bounding box
    tiles_info = []
    for x_str, y_dict in atlas_data.items():
        x_int = int(x_str)
        for y_str, tile_info in y_dict.items():
            y_int = int(y_str)
            w = tile_info["width"]
            h = tile_info["height"]

            tile_xmin = x_int
            tile_ymin = y_int
            tile_xmax = x_int + w
            tile_ymax = y_int + h

            if bboxes_intersect(tile_xmin, tile_ymin, tile_xmax, tile_ymax,
                                bxmin, bymin, bxmax, bymax):
                tiles_info.append({
                    "filename": tile_info["filename"],
                    "xmin": tile_xmin,
                    "ymin": tile_ymin,
                    "xmax": tile_xmax,
                    "ymax": tile_ymax
                })

    if not tiles_info:
        raise HTTPException(
            status_code=404,
            detail="No lidar tiles intersect the requested bounding box."
        )

    # 3) Return the list of intersecting tiles in JSON
    return {
        "message": "Success",
        "num_tiles": len(tiles_info),
        "tiles": tiles_info
    }

    # If you prefer returning a ZIP file of .laz binaries:
    #
    # mem_zip = io.BytesIO()
    # with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
    #     for tile in tiles_info:
    #         laz_name = tile["filename"]
    #         laz_path = os.path.join(LAZ_DIRECTORY, laz_name)
    #         if os.path.exists(laz_path):
    #             zf.write(laz_path, arcname=laz_name)
    #
    # mem_zip.seek(0)
    # from fastapi.responses import StreamingResponse
    # return StreamingResponse(
    #     mem_zip,
    #     media_type="application/x-zip-compressed",
    #     headers={"Content-Disposition": "attachment; filename=lidar_tiles.zip"}
    # )

# ------------------------------------------------------------------------
# 5. New endpoint: /get/lidar/{filename}
# ------------------------------------------------------------------------
@app.get("/get/lidar/{filename}")
def get_lidar_file(filename: str):
    """
    Returns the .laz file as a binary stream.
    Example: GET /get/lidar/foo.laz
    """
    laz_path = os.path.join(LAZ_DIRECTORY, filename)

    if not os.path.exists(laz_path):
        raise HTTPException(
            status_code=404,
            detail=f"Lidar file not found: {filename}"
        )

    # Return the file
    return FileResponse(path=laz_path, media_type="application/octet-stream", filename=filename)
