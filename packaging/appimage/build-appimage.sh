#!/bin/bash
set -euo pipefail

VERSION="${1:-0.1.1-beta}"
ARCH="x86_64"
APPDIR="Wrekker.AppDir"
PYTHON_VERSION="3.11.9"
PYTHON_BUILD_DATE="20240415"

rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin" "${APPDIR}/usr/lib" \
         "${APPDIR}/usr/share/applications" \
         "${APPDIR}/usr/share/metainfo" \
         "${APPDIR}/usr/share/icons/hicolor/256x256/apps" \
         "${APPDIR}/usr/share/icons/hicolor/scalable/apps"

curl -L "https://github.com/indygreg/python-build-standalone/releases/download/${PYTHON_BUILD_DATE}/cpython-${PYTHON_VERSION}+${PYTHON_BUILD_DATE}-x86_64-unknown-linux-gnu-install_only.tar.gz" \
  | tar -xz -C "${APPDIR}/usr/"

PYTHON="${APPDIR}/usr/python/bin/python3.11"
"${PYTHON}" -m pip install --upgrade pip
"${PYTHON}" -m pip install PyQt6 librosa soundfile numpy scipy sounddevice mutagen mido python-rtmidi structlog rich

cd wrekker/engine_rs
# --compatibility linux: skip the manylinux repair. It vendors libasound into
# the wheel, and a foreign libasound carries its build distro's ALSA plugin
# paths — on other distros snd_pcm_open then fails (no PipeWire/Pulse plugin).
# libasound must always come from the host; rubberband/samplerate are bundled
# into AppDir/usr/lib below instead.
maturin build --release --compatibility linux --interpreter "../../${PYTHON}" --out /tmp/wrekker-wheels
cd ../..
"${PYTHON}" -m pip install /tmp/wrekker-wheels/wrekker_engine-*.whl

"${PYTHON}" -m pip install --no-deps .

cp /usr/lib/librubberband.so* "${APPDIR}/usr/lib/" 2>/dev/null || \
cp /usr/lib/x86_64-linux-gnu/librubberband.so* "${APPDIR}/usr/lib/" 2>/dev/null || \
  echo "WARNING: librubberband not found; AppImage may require system librubberband"
cp /usr/lib/libsamplerate.so* "${APPDIR}/usr/lib/" 2>/dev/null || \
cp /usr/lib/x86_64-linux-gnu/libsamplerate.so* "${APPDIR}/usr/lib/" 2>/dev/null || \
  echo "WARNING: libsamplerate not found; AppImage may require system libsamplerate"

cat > "${APPDIR}/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="${HERE}/usr/python/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH:-}"
export PYTHONPATH="${HERE}/usr/python/lib/python3.11/site-packages:${PYTHONPATH:-}"
export WREKKER_MODELS_PATH="${HOME}/.local/share/wrekker/models"
# Wizard-installed packages: resolved per interpreter version by
# wrekker.config.paths.python_packages_dir() — do not override it here.
exec "${HERE}/usr/python/bin/python3.11" -m wrekker.ui.app "$@"
EOF
chmod +x "${APPDIR}/AppRun"

cat > "${APPDIR}/usr/share/applications/io.github.wrekker.Wrekker.desktop" << EOF
[Desktop Entry]
Name=Wrekker
Comment=Stem-native DJ software
Exec=wrekker
Icon=wrekker
Type=Application
Categories=AudioVideo;Audio;
EOF

if [ -f packaging/flatpak/io.github.wrekker.Wrekker.appdata.xml ]; then
  # Exactly one metainfo file, named after the AppStream component id —
  # appstreamcli flags any other filename and appimagetool aborts on it.
  cp packaging/flatpak/io.github.wrekker.Wrekker.appdata.xml \
    "${APPDIR}/usr/share/metainfo/io.github.wrekker.Wrekker.appdata.xml"
fi

if [ -f assets/icon-256.png ]; then
  cp assets/icon-256.png "${APPDIR}/usr/share/icons/hicolor/256x256/apps/wrekker.png"
  cp assets/icon-256.png "${APPDIR}/wrekker.png"
elif [ -f "wrekker logo.svg" ]; then
  cp "wrekker logo.svg" "${APPDIR}/usr/share/icons/hicolor/scalable/apps/wrekker.svg"
  cp "wrekker logo.svg" "${APPDIR}/wrekker.svg"
elif [ -f "wrekker/ui/assets/wrekker logo.svg" ]; then
  cp "wrekker/ui/assets/wrekker logo.svg" "${APPDIR}/usr/share/icons/hicolor/scalable/apps/wrekker.svg"
  cp "wrekker/ui/assets/wrekker logo.svg" "${APPDIR}/wrekker.svg"
else
  # Last-resort valid icon so appimagetool does not abort on clean repos that
  # have not generated raster assets yet.
  "${PYTHON}" - <<'PY' > "${APPDIR}/wrekker.xpm"
print("""/* XPM */
static char * wrekker_xpm[] = {
"32 32 2 1",
" 	c None",
".	c #F28C28",
"................................",
"................................",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"....      ....      ....      ....",
"................................",
"................................",
"................................",
"................................"
};""")
PY
fi
ln -sf usr/share/applications/io.github.wrekker.Wrekker.desktop \
  "${APPDIR}/io.github.wrekker.Wrekker.desktop"

wget -q -O appimagetool-x86_64.AppImage \
  "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
chmod +x appimagetool-x86_64.AppImage
APPIMAGE_EXTRACT_AND_RUN=1 ./appimagetool-x86_64.AppImage "${APPDIR}" "Wrekker-${VERSION}-${ARCH}.AppImage"
