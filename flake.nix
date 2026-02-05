{
  description = "Home Manager configuration for safareli";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, ... }:
    let
      system = "aarch64-linux";
      versions = import ./versions.nix;
      pkgs = import nixpkgs {
        inherit system;
        overlays = [
          (final: prev: {
            # claude-code - https://claude.ai/install.sh
            claude-code = prev.stdenv.mkDerivation rec {
              pname = "claude-code";
              inherit (versions.claude-code) version;

              src = prev.fetchurl {
                url = "https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases/${version}/linux-arm64/claude";
                inherit (versions.claude-code) sha256;
              };

              dontUnpack = true;
              dontPatchELF = true;
              dontStrip = true;

              installPhase = ''
                runHook preInstall
                mkdir -p $out/bin
                cp $src $out/bin/claude
                chmod +x $out/bin/claude
                runHook postInstall
              '';

              meta = {
                description = "Claude Code - Anthropic's AI coding assistant";
                homepage = "https://claude.ai";
                platforms = [ "aarch64-linux" ];
              };
            };

            # opencode - https://github.com/anomalyco/opencode
            opencode = prev.stdenv.mkDerivation rec {
              pname = "opencode";
              inherit (versions.opencode) version;

              src = prev.fetchurl {
                url = "https://github.com/anomalyco/opencode/releases/download/v${version}/opencode-linux-arm64.tar.gz";
                inherit (versions.opencode) sha256;
              };

              sourceRoot = ".";

              unpackPhase = ''
                tar -xzf $src
              '';

              # Don't use autoPatchelfHook - it breaks the bundled Bun executable
              dontPatchELF = true;
              dontStrip = true;

              installPhase = ''
                runHook preInstall
                mkdir -p $out/bin
                cp opencode $out/bin/
                chmod +x $out/bin/opencode
                runHook postInstall
              '';

              meta = {
                description = "OpenCode AI coding assistant";
                homepage = "https://github.com/anomalyco/opencode";
                platforms = [ "aarch64-linux" ];
              };
            };

            # pi (coding-agent) - https://github.com/badlogic/pi-mono
            pi = prev.stdenv.mkDerivation rec {
              pname = "pi";
              inherit (versions.pi) version;

              src = prev.fetchzip {
                url = "https://github.com/badlogic/pi-mono/releases/download/v${version}/pi-linux-arm64.tar.gz";
                inherit (versions.pi) sha256;
                stripRoot = false;
              };

              nativeBuildInputs = [ prev.autoPatchelfHook ];

              installPhase = ''
                runHook preInstall
                mkdir -p $out/bin $out/share/pi
                cp -r pi/* $out/share/pi/
                ln -s $out/share/pi/pi $out/bin/pi
                runHook postInstall
              '';

              meta = {
                description = "Pi coding agent";
                homepage = "https://github.com/badlogic/pi-mono";
                platforms = [ "aarch64-linux" ];
              };
            };
          })
        ];
        config.allowUnfree = true;
      };
    in
    {
      homeConfigurations."safareli" = home-manager.lib.homeManagerConfiguration {
        inherit pkgs;
        modules = [ ./home.nix ];
      };
    };
}
