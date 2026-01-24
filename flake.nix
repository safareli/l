{
  description = "Home Manager configuration for safareli";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    claude-code = {
      url = "github:sadjow/claude-code-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    opencode = {
      url = "github:anomalyco/opencode?ref=v1.1.34";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { nixpkgs, home-manager, claude-code, opencode, ... }:
    let
      system = "aarch64-linux";
      pkgs = import nixpkgs {
        inherit system;
        overlays = [
          claude-code.overlays.default
          (final: prev: {
            opencode = opencode.packages.${system}.default;

            # pi (coding-agent) - https://github.com/badlogic/pi-mono
            pi = prev.stdenv.mkDerivation rec {
              pname = "pi";
              version = "0.49.3";

              src = prev.fetchzip {
                url = "https://github.com/badlogic/pi-mono/releases/download/v${version}/pi-linux-arm64.tar.gz";
                sha256 = "sha256-OuExtug5WJyqbzyIrfxcfXZAWS5egAeeYsYXoPXAzrQ=";
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
