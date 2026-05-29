from unittest.mock import patch


def test_pip_install_detected_when_no_git_dir(tmp_path):
    """When PROJECT_ROOT has no .git, detect as pip install."""
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path):
        from hermes_cli.config import detect_install_method
        method = detect_install_method(project_root=tmp_path)
        assert method == "pip"


def test_git_install_detected_when_git_dir_exists(tmp_path):
    """When PROJECT_ROOT has .git, detect as git install."""
    (tmp_path / ".git").mkdir()
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path):
        from hermes_cli.config import detect_install_method
        method = detect_install_method(project_root=tmp_path)
        assert method == "git"


def test_managed_install_takes_precedence(tmp_path):
    """When HERMES_MANAGED is set, that takes precedence over git detection."""
    (tmp_path / ".git").mkdir()
    with patch("hermes_cli.config.get_managed_system", return_value="NixOS"), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path):
        from hermes_cli.config import detect_install_method
        method = detect_install_method(project_root=tmp_path)
        assert method == "nixos"


def test_recommended_update_command_pip():
    """Pip installs recommend pip install --upgrade."""
    from hermes_cli.config import recommended_update_command_for_method
    cmd = recommended_update_command_for_method("pip")
    assert "pip install" in cmd or "uv pip install" in cmd
    assert "--upgrade" in cmd
    assert "hermes-agent" in cmd


def test_stamp_file_takes_precedence(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".install_method").write_text("docker\n")
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path):
        from hermes_cli.config import detect_install_method
        assert detect_install_method(project_root=tmp_path) == "docker"


def test_official_docker_image_detected_via_build_sha(tmp_path):
    """The official published image is identified by its baked-in build SHA.

    ``.hermes_build_sha`` is written only by this repo's Dockerfile, so its
    presence is a reliable "this is the published image" signal even when the
    container has no ``.git`` tree.
    """
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path), \
         patch("hermes_cli.build_info.get_build_sha", return_value="a1eaad2f"):
        from hermes_cli.config import detect_install_method
        assert detect_install_method(project_root=tmp_path) == "docker"


def test_official_docker_image_detected_via_env(tmp_path):
    """HERMES_DOCKER_IMAGE=1 is the explicit, fork-friendly published-image signal."""
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path), \
         patch("hermes_cli.build_info.get_build_sha", return_value=None), \
         patch.dict("os.environ", {"HERMES_DOCKER_IMAGE": "1"}):
        from hermes_cli.config import detect_install_method
        assert detect_install_method(project_root=tmp_path) == "docker"


def test_generic_container_with_git_is_not_docker(tmp_path):
    """Regression for #34397: a regular git install inside a user's own
    container must NOT be classified as the published Docker image —
    otherwise ``hermes update`` wrongly refuses to update.
    """
    (tmp_path / ".git").mkdir()
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path), \
         patch("hermes_cli.build_info.get_build_sha", return_value=None), \
         patch.dict("os.environ", {}, clear=False), \
         patch("hermes_constants.is_container", return_value=True):
        import os
        os.environ.pop("HERMES_DOCKER_IMAGE", None)
        from hermes_cli.config import detect_install_method
        assert detect_install_method(project_root=tmp_path) == "git"


def test_generic_container_without_git_is_pip(tmp_path):
    """A pip install inside a user's own container resolves to 'pip', so
    ``hermes update`` routes to pip upgrade rather than the docker bail-out.
    """
    with patch("hermes_cli.config.get_managed_system", return_value=None), \
         patch("hermes_cli.config.get_hermes_home", return_value=tmp_path), \
         patch("hermes_cli.build_info.get_build_sha", return_value=None), \
         patch("hermes_constants.is_container", return_value=True):
        import os
        os.environ.pop("HERMES_DOCKER_IMAGE", None)
        from hermes_cli.config import detect_install_method
        assert detect_install_method(project_root=tmp_path) == "pip"


def test_recommended_update_command_docker():
    from hermes_cli.config import recommended_update_command_for_method
    assert "docker pull" in recommended_update_command_for_method("docker")
