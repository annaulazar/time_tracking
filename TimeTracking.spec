analysis = Analysis(
    ["time_tracking/__main__.py"],
    pathex=[SPECPATH],
)
archive = PYZ(analysis.pure)
executable = EXE(
    archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="TimeTracking",
    console=False,
    upx=False,
)
distribution = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    name="TimeTracking",
    upx=False,
)
app = BUNDLE(
    distribution,
    name="Учёт времени.app",
    bundle_identifier="io.github.annaulazar.time-tracking",
    version="1.0.0",
    info_plist={"CFBundleDisplayName": "Учёт времени"},
)
