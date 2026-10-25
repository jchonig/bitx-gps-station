#!/bin/sh
# build-apt-repo.sh DEBDIR OUTDIR
#
# Builds a one-suite apt repository ("stable", component "main") in OUTDIR
# from the .deb files in DEBDIR, ready to publish as a static site such as
# GitHub Pages. If APT_SIGNING_KEY holds an armored GPG private key, the
# Release file is signed (InRelease and Release.gpg) and the public key is
# written to OUTDIR/gps-station.gpg.
set -eu

debdir=$1
out=$2
suite=stable
archs="armhf arm64 all"

rm -rf "$out"
mkdir -p "$out/pool/main"
cp "$debdir"/*.deb "$out/pool/main/"

cd "$out"
for arch in $archs; do
    dir=dists/$suite/main/binary-$arch
    mkdir -p "$dir"
    apt-ftparchive --arch "$arch" packages pool > "$dir/Packages"
    gzip -9kn "$dir/Packages"
done

cd "dists/$suite"
apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=bitx-gps-station \
    -o APT::FTPArchive::Release::Label=bitx-gps-station \
    -o APT::FTPArchive::Release::Suite="$suite" \
    -o APT::FTPArchive::Release::Codename="$suite" \
    -o APT::FTPArchive::Release::Components=main \
    -o APT::FTPArchive::Release::Architectures="$archs" \
    release . > ../Release.tmp
mv ../Release.tmp Release
cd ../..

if [ -n "${APT_SIGNING_KEY:-}" ]; then
    GNUPGHOME=$(mktemp -d)
    export GNUPGHOME
    printf '%s\n' "$APT_SIGNING_KEY" | gpg --batch --quiet --import
    gpg --batch --yes --clearsign -o "dists/$suite/InRelease" "dists/$suite/Release"
    gpg --batch --yes -abs -o "dists/$suite/Release.gpg" "dists/$suite/Release"
    gpg --batch --export > gps-station.gpg
    rm -rf "$GNUPGHOME"
else
    echo "APT_SIGNING_KEY not set: repository is unsigned" >&2
fi

cat > index.html <<'EOF'
<!doctype html>
<meta charset="utf-8">
<title>bitx-gps-station apt repository</title>
<p>apt repository for
<a href="https://github.com/jchonig/bitx-gps-station">bitx-gps-station</a>.
See its README for installation instructions.</p>
EOF
