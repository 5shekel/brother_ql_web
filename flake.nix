{
  inputs = {
    utils.url = "github:numtide/flake-utils";
  };
  outputs =
    {
      self,
      nixpkgs,
      utils,
    }:
    utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        pythonWithDeps =
          with pkgs;
          (python3.withPackages (
            s: with s; [
              streamlit
              pillow
              pyusb
              qrcode
              requests
              (brother-ql.overrideAttrs {
                src = fetchFromGitHub {
                  owner = "matmair";
                  repo = "brother_ql-inventree";
                  rev = "ceb53b8d7b1b22f5300bbb33ac579eedf554b3ff";
                  hash = "sha256-bT0JaGxMCkN73GTz4PKLNht5LLzberAltKNV/wNluv0=";
                };
              })
            ]
          ));
      in
      {

        devShell = pkgs.mkShell {
          buildInputs = [
            pythonWithDeps
            pkgs.nixfmt-rfc-style
          ];
        };

        nixosModules.default =
          { config, ... }:
          {
            systemd.services.printit = {
              description = "PrintIT now, baby!";
              after = [ "network.target" ];
              wantedBy = [ config.systemd.defaultUnit ];
              path = [ pythonWithDeps ];
              script = ''
                python -m streamlit run ${./.}/printit.py --server.fileWatcherType none
              '';
              serviceConfig = {
                WorkingDirectory = "%S/printit";
                StateDirectory = "printit";
              };
            };
          };
      }
    );
}
