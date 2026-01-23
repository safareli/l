{ config, pkgs, lib, ... }:

{
  # Home Manager needs a bit of information about you and the paths it should manage
  home.username = "safareli";
  home.homeDirectory = "/home/safareli";

  # This value determines the Home Manager release compatibility
  home.stateVersion = "24.11";

  # Let Home Manager install and manage itself
  programs.home-manager.enable = true;

  # ============================================================================
  # Packages
  # ============================================================================
  home.packages = with pkgs; [
    # Core development
    nodejs_25          # Latest (project flakes override with nodejs_22 LTS)
    bun

    # AI tools
    claude-code-bun    # from github:sadjow/claude-code-nix (faster, uses Bun)

    # CLI tools
    jq
    lazygit
    fzf
    ripgrep
    fd
    bat                # better cat
    eza                # better ls
    htop
    tree
    curl
    wget

    # Process management
    process-compose    # docker-compose for processes

    # JSON viewers
    # otree            # Not in nixpkgs - install via: cargo install otree

    # Nix tools
    nil                # Nix LSP
    nixpkgs-fmt        # Nix formatter
  ];

  # ============================================================================
  # Shell - Zsh
  # ============================================================================
  programs.zsh = {
    enable = true;

    # Oh My Zsh
    oh-my-zsh = {
      enable = true;
      theme = "robbyrussell";
      plugins = [
        "git"
        "docker"
        "fzf"
        "z"              # directory jumping
        "history"
      ];
    };

    initContent = ''
      # Custom prompt with user@host prefix
      PROMPT="%F{green}%n@%m%f $PROMPT"

      # PATH additions
      export PATH="$HOME/.npm-global/bin:$HOME/.local/bin:$PATH"

      # fzf keybindings (Ctrl+R for history, Ctrl+T for files)
      if [ -n "''${commands[fzf-share]}" ]; then
        source "$(fzf-share)/key-bindings.zsh"
        source "$(fzf-share)/completion.zsh"
      fi
    '';

    shellAliases = {
      ll = "eza -la";
      la = "eza -a";
      ls = "eza";
      cat = "bat";
      lg = "lazygit";
      claude = "claude-bun";
      g = "git";

      # Nix shortcuts
      hms = "home-manager switch --flake ~/.config/home-manager";
      hmu = "nix flake update ~/.config/home-manager && home-manager switch --flake ~/.config/home-manager";
    };
  };

  # ============================================================================
  # Direnv
  # ============================================================================
  programs.direnv = {
    enable = true;
    enableZshIntegration = true;
    nix-direnv.enable = true;  # Faster nix-shell/flake loading with caching
  };

  # ============================================================================
  # Git
  # ============================================================================
  programs.git = {
    enable = true;
    # Uncomment and customize:
    # userName = "Your Name";
    # userEmail = "your.email@example.com";

    settings = {
      init.defaultBranch = "main";
      push.autoSetupRemote = true;
      pull.rebase = true;

      alias = {
        st = "status";
        co = "checkout";
        br = "branch";
        ci = "commit";
        lg = "log --oneline --graph --decorate";
      };
    };
  };

  # ============================================================================
  # fzf
  # ============================================================================
  programs.fzf = {
    enable = true;
    enableZshIntegration = true;
    defaultOptions = [
      "--height 40%"
      "--layout=reverse"
      "--border"
    ];
  };

  # ============================================================================
  # VS Code Server settings (for remote dev)
  # ============================================================================
  home.file.".vscode-server/data/Machine/settings.json".text = builtins.toJSON {
    "terminal.integrated.defaultProfile.linux" = "zsh";
  };

  # ============================================================================
  # npm global packages (installed on each home-manager switch)
  # ============================================================================
  home.activation.npmGlobalPackages = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
    export PATH="${pkgs.nodejs_25}/bin:$HOME/.npm-global/bin:$PATH"
    export NPM_CONFIG_PREFIX="$HOME/.npm-global"

    # Install/update global npm packages
    ${pkgs.nodejs_25}/bin/npm install -g opencode-ai
  '';
}
