{pkgs}: {
  deps = [
    pkgs.libgbm
    pkgs.eudev
    pkgs.xorg.libxcb
    pkgs.xorg.libXext
    pkgs.xorg.libX11
    pkgs.dbus
    pkgs.nspr
    pkgs.expat
    pkgs.glib
    pkgs.pango
    pkgs.gtk3
    pkgs.alsa-lib
    pkgs.mesa
    pkgs.xorg.libXrandr
    pkgs.xorg.libXfixes
    pkgs.xorg.libXdamage
    pkgs.xorg.libXcomposite
    pkgs.libxkbcommon
    pkgs.libdrm
    pkgs.cups
    pkgs.at-spi2-atk
    pkgs.atk
    pkgs.nss
  ];
}
