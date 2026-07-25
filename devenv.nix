{ pkgs, lib, config, inputs, ... }:

{
  packages = [ pkgs.git ];

  languages.python = {
    enable = true;
    venv = {
      enable = true;
      requirements = ./requirements.txt;
    };
  };

  processes = {
    # The three nodes exclude gateway/ and tests/ from --reload; without this,
    # every gateway edit restarts all three nodes and re-validates 2700 records.
    node-bch = {
      exec = "uvicorn main:app --port 8001 --reload --reload-exclude 'gateway/*' --reload-exclude 'tests/*'";
      env.HOSPITAL_NODE = "BCH";
      ready.http = {
        get.port = 8001;
        get.path = "/health";
      };
    };
    node-mgh = {
      exec = "uvicorn main:app --port 8002 --reload --reload-exclude 'gateway/*' --reload-exclude 'tests/*'";
      env.HOSPITAL_NODE = "MGH";
      ready.http = {
        get.port = 8002;
        get.path = "/health";
      };
    };
    node-bwh = {
      exec = "uvicorn main:app --port 8003 --reload --reload-exclude 'gateway/*' --reload-exclude 'tests/*'";
      env.HOSPITAL_NODE = "BWH";
      ready.http = {
        get.port = 8003;
        get.path = "/health";
      };
    };
    gateway = {
      exec = "uvicorn gateway.main:app --port 8000 --reload --reload-dir gateway";
      # devenv 2.0+ uses the `native` process manager, where ordering is
      # expressed with `after` (@ready is the default suffix for processes).
      # `process-compose.depends_on` is silently ignored unless
      # process.manager.implementation is set to "process-compose".
      # This is startup polish only: the gateway fans out per request, so it
      # serves fine when started before the nodes — they just report as
      # unreachable in the `sources` block until they come up.
      after = [
        "devenv:processes:node-bch@ready"
        "devenv:processes:node-mgh@ready"
        "devenv:processes:node-bwh@ready"
      ];
      ready.http = {
        get.port = 8000;
        get.path = "/health";
      };
    };
  };
}
