from cartridge.ids import game_id, is_game_id, rel_path_from_abs, sort_title


def test_game_id_shape() -> None:
    gid = game_id("SNES", "Super Metroid (USA).sfc")
    assert is_game_id(gid)
    assert len(gid) == 16


def test_same_rom_same_id_regardless_of_form() -> None:
    assert game_id("SNES", "a/b.sfc") == game_id("SNES", "/a/b.sfc") == game_id("SNES", "a\\b.sfc")
    assert game_id("SNES", "a//b.sfc") == game_id("SNES", "a/b.sfc")
    assert game_id("SNES", "Façade.sfc") == game_id("SNES", "Façade.sfc")


def test_system_is_part_of_identity() -> None:
    assert game_id("SNES", "x.bin") != game_id("Genesis", "x.bin")


def test_rel_path_from_abs() -> None:
    abs_path = "/media/fat/games/SNES/Sub/a.sfc"
    assert rel_path_from_abs(abs_path, ("/media/fat/games/",)) == "Sub/a.sfc"
    assert rel_path_from_abs(abs_path, ("/media/", "/media/fat/games/")) == "Sub/a.sfc"
    assert rel_path_from_abs(abs_path, ("/userdata/roms/",)) is None
    assert rel_path_from_abs(abs_path, ()) is None


def test_rel_path_from_abs_same_on_both_devices() -> None:
    a = rel_path_from_abs("/media/fat/games/SNES/a.sfc", ("/media/fat/games",))
    b = rel_path_from_abs("/userdata/roms/snes/a.sfc", ("/userdata/roms",))
    assert a == b == "a.sfc"


def test_sort_title() -> None:
    assert sort_title("The Legend of Zelda") == "legend of zelda"
    assert sort_title("A Link to the Past") == "link to the past"
    assert sort_title("Écran") == "ecran"


def test_is_game_id_rejects() -> None:
    for bad in ("ABCDEFGHIJKLMNOP", "a" * 15, "0" * 16, "../../etc/passwd", game_id("SNES", "x") + "\n"):
        assert not is_game_id(bad)
