{ stdenv }:

stdenv.mkDerivation {
  pname = "cpal";
  version = "0.1.0";
  src = ./.;

  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    $CC -O3 -pthread -o cpal main.c
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 cpal $out/bin/cpal
    runHook postInstall
  '';
}
