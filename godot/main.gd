extends Node2D

const HOST := "127.0.0.1"
const PORT := 8765
const CITY_TEXTURE := preload("res://art/city_nov_city.png")
const MAP_FONT := preload("res://art/LiberationSerif-Regular.ttf")
const CELL_W := 46.0
const CELL_H := 23.0
const SEA_LEVEL := 0.34
const HEIGHT_SCALE := 58.0

var tcp := StreamPeerTCP.new()
var buffer := ""
var tiles: Array = []
var cities: Array = []
var routes: Array = []
var tile_index := {}
var map_width := 0
var map_height := 0
var zoom := 1.0
var camera := Vector2.ZERO
var dragging := false
var drag_last := Vector2.ZERO
var shimmer := 0.0
var status := "Подключение к карте…"
var map_ready := false

const BIOME_COLORS := {
	"water": Color("#2d5d72"), "deep_water": Color("#214b60"), "river": Color("#3e7480"),
	"forest": Color("#4e6d55"), "rainforest": Color("#416653"), "temperate_forest": Color("#58715a"),
	"mountain": Color("#777b78"), "hills": Color("#857f62"), "desert": Color("#9f956e"),
	"tundra": Color("#899695"), "swamp": Color("#587166"), "plain": Color("#8d9a70"),
	"grassland": Color("#7f976d"), "steppe": Color("#92916b"), "snow": Color("#abb4ae")
}

func _ready() -> void:
	RenderingServer.set_default_clear_color(Color("#111b20"))
	tcp.connect_to_host(HOST, PORT)
	get_viewport().size_changed.connect(_on_viewport_resized)
	queue_redraw()

func _process(delta: float) -> void:
	shimmer = fmod(shimmer + delta, TAU)
	tcp.poll()
	if tcp.get_status() == StreamPeerTCP.STATUS_CONNECTED and status.begins_with("Подключение"):
		tcp.put_data((JSON.stringify({"type": "get_map", "version": 2}) + "\n").to_utf8_buffer())
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
	tile_index.clear()
	for tile in tiles:
		tile_index[Vector2i(int(tile.get("x", 0)), int(tile.get("y", 0)))] = tile
	map_ready = true
	status = "Мир загружен · %d клеток · %d городов · %d маршрутов" % [tiles.size(), cities.size(), routes.size()]
	_fit_map()

func _fit_map() -> void:
	if tiles.is_empty():
		return
	var bounds := _map_bounds(1.0, true)
	var viewport := get_viewport_rect().size
	var usable := Rect2(24.0, 84.0, viewport.x - 48.0, viewport.y - 126.0)
	zoom = clamp(min(usable.size.x / max(bounds.size.x, 1.0), usable.size.y / max(bounds.size.y, 1.0)), 0.45, 2.0)
	bounds = _map_bounds(zoom, true)
	camera = usable.position + (usable.size - bounds.size) * 0.5 - bounds.position

func _map_bounds(scale: float, land_only := false) -> Rect2:
	var bounds := Rect2()
	var first := true
	for tile in tiles:
		if land_only and str(tile.get("terrain", "plain")) == "water":
			continue
		for point in _tile_points(tile, scale):
			if first:
				bounds = Rect2(point, Vector2.ZERO)
				first = false
			else:
				bounds = bounds.expand(point)
	return bounds

func _tile_center(tile: Dictionary, scale := zoom) -> Vector2:
	var x := float(tile.get("x", 0))
	var y := float(tile.get("y", 0))
	return camera + Vector2((x - y) * CELL_W * 0.5, (x + y) * CELL_H * 0.5) * scale

func _tile_points(tile: Dictionary, scale := zoom) -> PackedVector2Array:
	var center := _tile_center(tile, scale)
	var elevation := float(tile.get("elevation", 0.0))
	var x := int(tile.get("x", 0))
	var y := int(tile.get("y", 0))
	return PackedVector2Array([
		center + Vector2(0, -CELL_H * 0.5 - elevation * HEIGHT_SCALE) * scale,
		center + Vector2(CELL_W * 0.5, -elevation * HEIGHT_SCALE) * scale,
		center + Vector2(0, CELL_H * 0.5 - elevation * HEIGHT_SCALE) * scale,
		center + Vector2(-CELL_W * 0.5, -elevation * HEIGHT_SCALE) * scale
	])

func _is_water(tile: Dictionary) -> bool:
	return str(tile.get("terrain", "")) == "water" or str(tile.get("terrain", "")) == "river"

func _tile_color(tile: Dictionary) -> Color:
	var terrain := str(tile.get("terrain", "plain"))
	var biome := str(tile.get("biome", terrain))
	return BIOME_COLORS.get(biome, BIOME_COLORS.get(terrain, BIOME_COLORS["plain"]))

func _draw() -> void:
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
	_draw_cities()

func _draw_tile(tile: Dictionary) -> void:
	var center := _tile_center(tile)
	var elevation := float(tile.get("elevation", 0.0))
	var shape := _tile_points(tile)
	var terrain := str(tile.get("terrain", "plain"))
	var color := _tile_color(tile)
	if terrain == "water":
		color = Color("#28566f")
	# Свет слева сверху, объём уходит вниз-вправо.
	if elevation > 0.025:
		var side := PackedVector2Array([shape[1], shape[2], shape[2] + Vector2(0, 5) * zoom, shape[1] + Vector2(0, 5) * zoom])
		draw_colored_polygon(side, color.darkened(0.42))
		var shadow := PackedVector2Array([shape[1] + Vector2(5, 4) * zoom, shape[2] + Vector2(5, 4) * zoom, shape[2] + Vector2(11, 8) * zoom, shape[1] + Vector2(11, 8) * zoom])
		draw_colored_polygon(shadow, Color(0.03, 0.05, 0.06, clamp(elevation * 0.22, 0.05, 0.24)))
	draw_colored_polygon(shape, color)
	draw_polyline(PackedVector2Array([shape[0], shape[1], shape[2], shape[3], shape[0]]), Color("#263b3b"), 1.0 * zoom)
	if terrain == "mountain" or str(tile.get("relief", "")) in ["mountains", "peaks"]:
		_draw_mountain(center, elevation, str(tile.get("relief", "mountains")))
	elif terrain == "hills" or elevation > 0.32:
		_draw_hill(center, elevation)
	if terrain == "water":
		var glint := center + Vector2(-8, -2) * zoom
		draw_line(glint, glint + Vector2(15, -2) * zoom, Color("#77a6aa"), 1.0 * zoom)

func _draw_hill(center: Vector2, elevation: float) -> void:
	var w := (12.0 + elevation * 14.0) * zoom
	var h := (7.0 + elevation * 12.0) * zoom
	draw_colored_polygon(PackedVector2Array([center + Vector2(-w, 3) * zoom, center + Vector2(0, -h), center + Vector2(w, 3) * zoom]), Color("#a39b72"))
	draw_line(center + Vector2(-w, 3) * zoom, center + Vector2(0, -h), Color("#d0bd83"), 1.0 * zoom)

func _draw_mountain(center: Vector2, elevation: float, relief: String) -> void:
	var h := (20.0 + elevation * 48.0) * zoom
	if relief == "peaks":
		h *= 1.22
	var w := (18.0 + elevation * 18.0) * zoom
	var base := center + Vector2(0, 3) * zoom
	draw_colored_polygon(PackedVector2Array([base + Vector2(-w, 0), base + Vector2(0, -h), base + Vector2(0, 4), base + Vector2(w * 0.85, 0)]), Color("#a2a59b"))
	draw_colored_polygon(PackedVector2Array([base + Vector2(0, -h), base + Vector2(w * 0.85, 0), base + Vector2(0, 4)]), Color("#4e5a5c"))
	draw_colored_polygon(PackedVector2Array([base + Vector2(-w * 0.25, -h * 0.48), base + Vector2(0, -h), base + Vector2(w * 0.2, -h * 0.45)]), Color("#e0ded0"))
	draw_line(base + Vector2(0, -h), base + Vector2(w * 0.85, 0), Color("#26383d"), 1.0 * zoom)

func _draw_route(route: Dictionary) -> void:
	var points := PackedVector2Array()
	for point in route.get("points", []):
		var tile = tile_index.get(Vector2i(int(point.get("x", 0)), int(point.get("y", 0))))
		if tile:
			points.append(_tile_center(tile) + Vector2(0, -2) * zoom)
	if points.size() > 1:
		draw_polyline(points, Color(0.08, 0.12, 0.12, 0.70), 5.0 * zoom, true)
		draw_polyline(points, Color("#d5b96d"), 1.5 * zoom, true)

func _draw_cities() -> void:
	var placed: Array = []
	var label_budget := 8 if zoom < 0.9 else 30
	for city in cities:
		var tile = tile_index.get(Vector2i(int(city.get("x", 0)), int(city.get("y", 0))))
		if not tile:
			continue
		var p := _tile_center(tile) + Vector2(0, -12) * zoom
		var size := Vector2(34, 34) * zoom
		draw_circle(p + Vector2(0, 2) * zoom, 5.0 * zoom, Color(0.02, 0.05, 0.06, 0.52))
		draw_texture_rect(CITY_TEXTURE, Rect2(p - size * 0.5, size), false, Color.WHITE)
		var label := str(city.get("name", "Город")).split(" — ")[0]
		var label_rect := Rect2(p + Vector2(9, -9) * zoom, Vector2(94, 17) * zoom)
		var crowded := false
		for other in placed:
			if label_rect.grow(4.0 * zoom).intersects(other):
				crowded = true
				break
		if not crowded and placed.size() < label_budget:
			draw_rect(label_rect.grow(3.0 * zoom), Color(0.05, 0.09, 0.10, 0.72))
			draw_string(MAP_FONT, label_rect.position + Vector2(3, 14) * zoom, label, HORIZONTAL_ALIGNMENT_LEFT, -1, maxi(11, int(14 * zoom)), Color("#f2e5bd"))
			placed.append(label_rect)

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
	if map_ready:
		_fit_map()
	queue_redraw()
