extends Node2D

const HOST := "127.0.0.1"
const PORT := 8765
const CITY_TEXTURE := preload("res://art/city_nov_city.png")
const MAP_FONT := preload("res://art/LiberationSerif-Regular.ttf")

var tcp := StreamPeerTCP.new()
var buffer := ""
var tiles: Array = []
var cities: Array = []
var routes: Array = []
var map_width := 0
var map_height := 0
var zoom := 1.0
var camera := Vector2.ZERO
var dragging := false
var drag_last := Vector2.ZERO
var shimmer := 0.0
var status := "Подключение к карте…"
var map_ready := false

const TERRAIN_COLORS := {
	"water": Color("#254c68"), "deep_water": Color("#1b3c56"),
	"forest": Color("#3f604d"), "mountain": Color("#747b7d"),
	"hills": Color("#817957"), "desert": Color("#a18a5e"),
	"tundra": Color("#778b8b"), "swamp": Color("#49675f"),
	"plain": Color("#9b956c"), "grassland": Color("#7e956d")
}

func _ready() -> void:
	tcp.connect_to_host(HOST, PORT)
	get_viewport().size_changed.connect(_on_viewport_resized)
	queue_redraw()

func _process(delta: float) -> void:
	shimmer = fmod(shimmer + delta, TAU)
	tcp.poll()
	if tcp.get_status() == StreamPeerTCP.STATUS_CONNECTED and status.begins_with("Подключение"):
		tcp.put_data((JSON.stringify({"type": "get_map"}) + "\n").to_utf8_buffer())
		status = "Соединение установлено · загрузка мира…"
	if tcp.get_status() == StreamPeerTCP.STATUS_ERROR:
		status = "TCP: ошибка соединения"
	while tcp.get_available_bytes() > 0:
		buffer += tcp.get_utf8_string(tcp.get_available_bytes())
		var end := buffer.find("\n")
		if end < 0:
			break
		var parsed = JSON.parse_string(buffer.substr(0, end))
		buffer = buffer.substr(end + 1)
		if parsed is Dictionary and parsed.get("type") == "map":
			_apply_map(parsed)
	queue_redraw()

func _apply_map(payload: Dictionary) -> void:
	tiles = payload.get("tiles", [])
	cities = payload.get("cities", [])
	routes = payload.get("routes", [])
	map_width = int(payload.get("width", 0))
	map_height = int(payload.get("height", 0))
	map_ready = true
	status = "Мир загружен · %d клеток · %d городов · %d маршрутов" % [tiles.size(), cities.size(), routes.size()]
	_fit_map()

func _fit_map() -> void:
	if tiles.is_empty():
		return
	var extent := float(maxi(1, map_width + map_height))
	zoom = clamp(min((get_viewport_rect().size.x - 150.0) / (extent * 1.55), (get_viewport_rect().size.y - 140.0) / (extent * 0.85)), 0.45, 1.8)
	camera = Vector2(get_viewport_rect().size.x * 0.5, 100.0)

func _tile_center(tile: Dictionary) -> Vector2:
	var x := float(tile.get("x", 0))
	var y := float(tile.get("y", 0))
	return camera + Vector2((x - y) * 48.0, (x + y) * 24.0) * zoom

func _tile_by_point(point: Dictionary) -> Dictionary:
	for tile in tiles:
		if int(tile.get("x", -1)) == int(point.get("x", -2)) and int(tile.get("y", -1)) == int(point.get("y", -2)):
			return tile
	return {}

func _tile_shape(center: Vector2, elevation: float, lift := 0.0) -> PackedVector2Array:
	var half_w := 48.0 * zoom
	var half_h := 24.0 * zoom
	var z := (elevation * 8.0 + lift) * zoom
	return PackedVector2Array([center + Vector2(0, -half_h - z), center + Vector2(half_w, -z), center + Vector2(0, half_h - z), center + Vector2(-half_w, -z)])

func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, get_viewport_rect().size), Color("#101820"))
	draw_string(MAP_FONT, Vector2(24, 34), "NOVERIA  /  POLITICAL MAP", HORIZONTAL_ALIGNMENT_LEFT, -1, 21, Color("#e5dcc3"))
	draw_string(MAP_FONT, Vector2(25, 59), status, HORIZONTAL_ALIGNMENT_LEFT, -1, 15, Color("#9caeae"))
	if not map_ready:
		draw_string(MAP_FONT, Vector2(25, 94), "Ожидание snapshot от Python…", HORIZONTAL_ALIGNMENT_LEFT, -1, 16, Color("#c5b98e"))
		return
	_draw_map()
	draw_string(MAP_FONT, Vector2(25, get_viewport_rect().size.y - 22), "Колесо · масштаб     Средняя кнопка / Shift + ЛКМ · панорама", HORIZONTAL_ALIGNMENT_LEFT, -1, 13, Color("#718384"))

func _draw_map() -> void:
	for tile in tiles:
		_draw_tile(tile)
	for route in routes:
		_draw_route(route)
	for city in cities:
		_draw_city(city)

func _draw_tile(tile: Dictionary) -> void:
	var center := _tile_center(tile)
	var elevation := float(tile.get("elevation", 0.0))
	var shape := _tile_shape(center, elevation)
	var terrain := str(tile.get("terrain", "plain"))
	var color: Color = TERRAIN_COLORS.get(terrain, TERRAIN_COLORS["plain"])
	if terrain == "water":
		var pulse := (sin(shimmer * 1.4 + float(tile.get("x", 0)) * 0.8 + float(tile.get("y", 0)) * 0.35) + 1.0) * 0.5
		color = color.lightened(pulse * 0.10)
	# A dark extrusion gives the elevated tile a restrained pseudo-3D edge.
	if elevation > 0.03:
		var lower := _tile_shape(center + Vector2(0, 6.0 * zoom), elevation, -7.0)
		draw_colored_polygon(lower, color.darkened(0.42))
		draw_line(lower[3], lower[2], Color("#18262b"), 1.0)
	# Directional cast shadow: upper-left light, shadow falls down-right.
	if elevation > 0.08:
		var shadow := PackedVector2Array([shape[1] + Vector2(8, 7) * zoom, shape[2] + Vector2(8, 7) * zoom, shape[2] + Vector2(15, 11) * zoom, shape[1] + Vector2(15, 11) * zoom])
		draw_colored_polygon(shadow, Color(0.03, 0.06, 0.07, clamp(elevation * 0.12, 0.04, 0.18)))
	draw_colored_polygon(shape, color)
	draw_polyline(PackedVector2Array([shape[0], shape[1], shape[2], shape[3], shape[0]]), Color(0.08, 0.13, 0.14, 0.42), 0.8)
	if terrain == "forest":
		_draw_forest(center, elevation)
	elif terrain == "mountain":
		_draw_mountain(center, elevation, str(tile.get("relief", "mountains")))
	if terrain == "water":
		var glint := center + Vector2(sin(shimmer + float(tile.get("x", 0))) * 12.0, -3.0) * zoom
		draw_line(glint - Vector2(8, 2) * zoom, glint + Vector2(8, -2) * zoom, Color(0.68, 0.82, 0.78, 0.22), 1.2)

func _draw_forest(center: Vector2, elevation: float) -> void:
	var count := 2 if elevation < 0.5 else 3
	for index in range(count):
		var p := center + Vector2((index - 1) * 15.0, -10.0 - float(index % 2) * 5.0) * zoom
		var h := (20.0 + elevation * 13.0 + float(index) * 3.0) * zoom
		var w := h * 0.42
		draw_colored_polygon(PackedVector2Array([p + Vector2(0, -h), p + Vector2(w, 0), p + Vector2(0, -h * 0.18), p + Vector2(-w, 0)]), Color("#29483d"))
		draw_colored_polygon(PackedVector2Array([p + Vector2(0, -h * 0.78), p + Vector2(w * 0.72, -h * 0.10), p + Vector2(0, -h * 0.32), p + Vector2(-w * 0.72, -h * 0.10)]), Color("#55765a"))

func _draw_mountain(center: Vector2, elevation: float, relief: String) -> void:
	var h := (22.0 + elevation * 50.0) * zoom
	if relief == "peaks":
		h *= 1.25
	var w := (26.0 + elevation * 18.0) * zoom
	var base := center + Vector2(0, 3) * zoom
	# Two faces make the height readable at map scale; the light is upper-left.
	draw_colored_polygon(PackedVector2Array([base + Vector2(-w, 0), base + Vector2(0, -h), base + Vector2(0, 4), base + Vector2(w * 0.82, 0)]), Color("#788184"))
	draw_colored_polygon(PackedVector2Array([base + Vector2(0, -h), base + Vector2(w * 0.82, 0), base + Vector2(0, 4)]), Color("#4c565b"))
	draw_line(base + Vector2(0, -h), base + Vector2(w * 0.82, 0), Color("#bac0b7"), 1.0 * zoom)

func _draw_route(route: Dictionary) -> void:
	var points := PackedVector2Array()
	for point in route.get("points", []):
		var tile := _tile_by_point(point)
		if not tile.is_empty():
			points.append(_tile_center(tile) + Vector2(0, -4) * zoom)
	if points.size() > 1:
		draw_polyline(points, Color(0.78, 0.63, 0.31, 0.30), 5.0 * zoom, true)
		draw_polyline(points, Color("#d5b96d"), 1.2 * zoom, true)

func _draw_city(city: Dictionary) -> void:
	var tile := _tile_by_point(city)
	if tile.is_empty():
		return
	var p := _tile_center(tile) + Vector2(0, -17) * zoom
	var size := Vector2(112, 112) * zoom
	draw_circle(p + Vector2(0, 4) * zoom, 12.0 * zoom, Color(0.02, 0.05, 0.06, 0.45))
	draw_texture_rect(CITY_TEXTURE, Rect2(p - size * 0.5, size), false, Color.WHITE)
	var label := str(city.get("name", "Город"))
	draw_string(MAP_FONT, p + Vector2(17, 4) * zoom, label, HORIZONTAL_ALIGNMENT_LEFT, -1, maxi(12, int(15 * zoom)), Color("#f2e5bd"))

func _input(event: InputEvent) -> void:
	if event is InputEventMouseButton:
		if event.button_index == MOUSE_BUTTON_WHEEL_UP:
			zoom = clamp(zoom * 1.1, 0.35, 3.0)
		elif event.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			zoom = clamp(zoom / 1.1, 0.35, 3.0)
		elif event.button_index == MOUSE_BUTTON_MIDDLE or (event.button_index == MOUSE_BUTTON_LEFT and event.shift_pressed):
			dragging = event.pressed
			drag_last = event.position
	elif event is InputEventMouseMotion and dragging:
		camera += event.position - drag_last
		drag_last = event.position
		queue_redraw()

func _on_viewport_resized() -> void:
	queue_redraw()
