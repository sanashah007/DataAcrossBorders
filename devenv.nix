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
    node-bch = {
      exec = "uvicorn main:app --port 8001 --reload";
      env.HOSPITAL_NODE = "BCH";
      ready.http = {
        get.port = 8001;
        get.path = "/health";
      };
    };
    node-mgh = {
      exec = "uvicorn main:app --port 8002 --reload";
      env.HOSPITAL_NODE = "MGH";
      ready.http = {
        get.port = 8002;
        get.path = "/health";
      };
    };
    node-bwh = {
      exec = "uvicorn main:app --port 8003 --reload";
      env.HOSPITAL_NODE = "BWH";
      ready.http = {
        get.port = 8003;
        get.path = "/health";
      };
    };
  };
}
