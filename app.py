import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse


app = FastAPI(
    title="Project Zomboid Map Generator",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Location of the existing PZMapCreation checkout.
PZMAPCREATION = Path(
    os.environ.get(
        "PZMAPCREATION",
        os.path.expanduser("~/Documents/PZMapCreation"),
    )
).resolve()

# Location of the Project Zomboid installation/media directory.
PZ_MEDIA = Path(
    os.environ.get(
        "PZ_MEDIA",
        os.path.expanduser(
            "~/.local/share/Steam/steamapps/common/"
            "ProjectZomboid/projectzomboid/media"
        ),
    )
).resolve()

# Java executable.
JAVA = os.environ.get("JAVA", "java")

# PZMapCreation's compiled classes.
PZ_OUT = PZMAPCREATION / "out"

# Maximum time allowed for one map generation.
GENERATION_TIMEOUT = int(
    os.environ.get("GENERATION_TIMEOUT", "1800")
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def validate_environment():
    """Verify that the existing PZMapCreation installation is usable."""

    if not PZMAPCREATION.is_dir():
        raise RuntimeError(
            f"PZMapCreation directory does not exist: {PZMAPCREATION}"
        )

    if not PZ_OUT.is_dir():
        raise RuntimeError(
            f"PZMapCreation compiled output does not exist: {PZ_OUT}"
        )

    if not PZ_MEDIA.is_dir():
        raise RuntimeError(
            f"Project Zomboid media directory does not exist: {PZ_MEDIA}"
        )


async def save_upload(upload: UploadFile, destination: Path):
    """Save an uploaded file to a known temporary path."""

    # We deliberately ignore the client's filename and use our own.
    with destination.open("wb") as output:
        while chunk := await upload.read(1024 * 1024):
            output.write(chunk)

    await upload.close()


def zip_directory(source: Path, destination: Path):
    """
    Create destination.zip from source.

    The resulting archive contains:
        PZGisImport/...
    """

    # source is .../mods/PZGisImport
    # We want the archive to contain PZGisImport/...
    shutil.make_archive(
        base_name=str(destination.with_suffix("")),
        format="zip",
        root_dir=source.parent,
        base_dir=source.name,
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        validate_environment()

        return {
            "status": "ok",
            "pzmapcreation": str(PZMAPCREATION),
            "pz_media": str(PZ_MEDIA),
        }

    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
        )


@app.post("/api/maps")
async def generate_map(
    buildings: UploadFile = File(...),
    roads: UploadFile = File(...),
    area: UploadFile = File(...),
):
    """
    Generate a Project Zomboid map using the existing PZMapCreation
    `pzformat.Probe giscells` command.
    """

    validate_environment()

    job_id = uuid.uuid4().hex

    # Everything for this request lives underneath this directory.
    work_dir = Path(
        tempfile.mkdtemp(prefix=f"pzmap-{job_id}-")
    )

    try:
        input_dir = work_dir / "input"
        mods_dir = work_dir / "mods"
        output_zip = work_dir / "PZGisImport.zip"

        input_dir.mkdir()
        mods_dir.mkdir()

        buildings_path = input_dir / "buildings.geojson"
        roads_path = input_dir / "roads.geojson"
        area_path = input_dir / "area.geojson"

        # Save uploaded files.
        await save_upload(buildings, buildings_path)
        await save_upload(roads, roads_path)
        await save_upload(area, area_path)

        # ------------------------------------------------------------------
        # THIS IS THE EXISTING PZMapCreation INVOCATION.
        #
        # Equivalent to:
        #
        # java -cp out pzformat.Probe giscells \
        #   buildings.geojson \
        #   roads.geojson \
        #   area.geojson \
        #   "$PZ/media" \
        #   ~/Zomboid/mods \
        #   PZGisImport
        # ------------------------------------------------------------------

        command = [
            JAVA,
            "-cp",
            str(PZ_OUT),
            "pzformat.Probe",
            "giscells",
            str(buildings_path),
            str(roads_path),
            str(area_path),
            str(PZ_MEDIA),
            str(mods_dir),
            "PZGisImport",
        ]

        try:
            result = subprocess.run(
                command,
                cwd=PZMAPCREATION,
                capture_output=True,
                text=True,
                timeout=GENERATION_TIMEOUT,
            )

        except subprocess.TimeoutExpired:
            raise HTTPException(
                status_code=504,
                detail=(
                    f"Map generation exceeded "
                    f"{GENERATION_TIMEOUT} seconds."
                ),
            )

        # Preserve the existing generator's stdout/stderr in server logs.
        if result.stdout:
            print(
                f"[{job_id}] PZMapCreation stdout:\n"
                f"{result.stdout}"
            )

        if result.stderr:
            print(
                f"[{job_id}] PZMapCreation stderr:\n"
                f"{result.stderr}"
            )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "PZMapCreation failed.",
                    "exit_code": result.returncode,
                    "stderr": result.stderr[-5000:],
                },
            )

        # The existing command should have created:
        #
        #   mods_dir/PZGisImport/
        #
        generated_mod = mods_dir / "PZGisImport"

        if not generated_mod.is_dir():
            raise HTTPException(
                status_code=500,
                detail=(
                    "PZMapCreation completed successfully, "
                    "but PZGisImport was not created."
                ),
            )

        # ZIP the generated mod.
        zip_directory(
            generated_mod,
            output_zip,
        )

        if not output_zip.is_file():
            raise HTTPException(
                status_code=500,
                detail="Failed to create output ZIP.",
            )

        return FileResponse(
            path=output_zip,
            media_type="application/zip",
            filename="PZGisImport.zip",
        )

    finally:
        # FileResponse needs the file to remain available while it is being
        # sent. FastAPI/Starlette may still be reading it when this function
        # returns, so cleanup is handled by a background task in production.
        #
        # For the initial version, don't remove work_dir here.
        #
        # See the note below.
        pass
