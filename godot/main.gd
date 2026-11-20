extends Node2D

const HOST := "127.0.0.1"
const PORT := 8765
var tcp := StreamPeerTCP.new()
var buffer := ""
var tiles: Array = []
var scale_map := 1.0
var offset := Vector2(40, 40)
var status := "connecting..."

func _ready() -> void:
	status = "connecting to %s:%d" % [HOST, PORT]
	tcp.connect_to_host(HOST, PORT)
	queue_redraw()

func _process(_delta: float) -> void:
	tcp.poll()
	if tcp.get_status() == StreamPeerTCP.STATUS_CONNECTED and status.begins_with("connecting"):
		tcp.put_data((JSON.stringify({"type": "get_map"}) + "\n").to_utf8_buffer())
		status = "connected; loading map"
	if tcp.get_available_bytes() > 0:
		buffer += tcp.get_utf8_string(tcp.get_available_bytes())
		var end := buffer.find("\n")
		if end >= 0:
			var parsed = JSON.parse_string(buffer.substr(0, end))
			buffer = buffer.substr(end + 1)
			if parsed is Dictionary and parsed.get("type") == "map":
				tiles = parsed.get("tiles", [])
				status = "map loaded: %d tiles" % tiles.size()
				_fit_map()
				queue_redraw()

func _fit_map() -> void:
	if tiles.is_empty(): return
	var viewport_size := get_viewport_rect().size
	var max_x := 0
	var max_y := 0
	for tile in tiles:
		max_x = maxi(max_x, int(tile.get("x", 0)))
		max_y = maxi(max_y, int(tile.get("y", 0)))
	scale_map = min((viewport_size.x - 80.0) / max(1, max_x + max_y), (viewport_size.y - 100.0) / max(1, max_x + max_y))
	offset = Vector2(viewport_size.x * 0.5, 55)

func _draw() -> void:
	draw_string(ThemeDB.fallback_font, Vector2(18, 26), "NOVERIA · GODOT MAP", HORIZONTAL_ALIGNMENT_LEFT, -1, 20, Color.WHITE)
	draw_string(ThemeDB.fallback_font, Vector2(18, 48), status, HORIZONTAL_ALIGNMENT_LEFT, -1, 14, Color("#a9bac7"))
	var tw := 2.0 * scale_map
	var th := scale_map
	for tile in tiles:
		var x := int(tile.get("x", 0))
		var y := int(tile.get("y", 0))
		var center := offset + Vector2((x - y) * tw, (x + y) * th * 0.5)
		var points := PackedVector2Array([center + Vector2(0, -th), center + Vector2(tw, 0), center + Vector2(0, th), center + Vector2(-tw, 0)])
		draw_colored_polygon(points, _tile_color(tile))

func _tile_color(tile: Dictionary) -> Color:
	match str(tile.get("terrain", "plain")):
		"water": return Color("#245276")
		"forest": return Color("#477b5d")
		"mountain": return Color("#858998")
		_: return Color("#b9b477")
