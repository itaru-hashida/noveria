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
var corner_heights := {}
var corner_colors := {}
var corner_water := {}
var terrain_mesh := ArrayMesh.new()
var terrain_mesh_instance: MeshInstance2D
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

const TERRAIN_SHADER := """
shader_type canvas_item;
render_mode unshaded;
varying vec2 map_position;

void vertex() { map_position = VERTEX; }

float soft_noise(vec2 p) {
	vec2 cell = floor(p);
	vec2 local = fract(p);
	local = local * local * (3.0 - 2.0 * local);
	float a = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453);
	float b = fract(sin(dot(cell + vec2(1.0, 0.0), vec2(12.9898, 78.233))) * 43758.5453);
	float c = fract(sin(dot(cell + vec2(0.0, 1.0), vec2(12.9898, 78.233))) * 43758.5453);
	float d = fract(sin(dot(cell + vec2(1.0, 1.0), vec2(12.9898, 78.233))) * 43758.5453);
	return mix(mix(a, b, local.x), mix(c, d, local.x), local.y);
}

void fragment() {
	float water = smoothstep(0.35, 0.65, UV.x);
	float grain = (soft_noise(map_position * 0.035) - 0.5) * 0.045;
	float broad = (soft_noise(map_position * 0.008) - 0.5) * 0.035;
	vec3 land = COLOR.rgb * (1.0 + grain + broad);
	vec3 water_dark = vec3(0.105, 0.255, 0.325);
	vec3 water_light = vec3(0.245, 0.455, 0.505);
	vec3 water_color = mix(water_dark, water_light, clamp(0.22 + UV.y * 0.72, 0.0, 1.0));
	water_color *= 1.0 + grain * 0.55;
	COLOR = vec4(mix(land, water_color, water), 1.0);
}
"""

func _ready() -> void:
	RenderingServer.set_default_clear_color(Color("#111b20"))
	terrain_mesh_instance = MeshInstance2D.new()
	terrain_mesh_instance.name = "ContinuousTerrainMesh"
	terrain_mesh_instance.z_index = -10
	terrain_mesh_instance.mesh = terrain_mesh
	var material := ShaderMaterial.new()
	var shader := Shader.new()
	shader.code = TERRAIN_SHADER
	material.shader = shader
	terrain_mesh_instance.material = material
	add_child(terrain_mesh_instance)
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
	_build_corner_heightfield()
	map_ready = true
	status = "Мир загружен · %d клеток · %d городов · %d маршрутов" % [tiles.size(), cities.size(), routes.size()]
	_fit_map()
	_rebuild_terrain_mesh()

func _build_corner_heightfield() -> void:
	corner_heights.clear()
	corner_colors.clear()
	corner_water.clear()
	for tile in tiles:
		var x := int(tile.get("x", 0))
		var y := int(tile.get("y", 0))
		for corner in [Vector2i(x, y), Vector2i(x + 1, y), Vector2i(x + 1, y + 1), Vector2i(x, y + 1)]:
			var samples: Array = []
			var colors: Array = []
			var water_samples: Array = []
			for cell in [Vector2i(corner.x, corner.y), Vector2i(corner.x - 1, corner.y), Vector2i(corner.x, corner.y - 1), Vector2i(corner.x - 1, corner.y - 1)]:
				if tile_index.has(cell):
					var sample: Dictionary = tile_index[cell]
					samples.append(float(sample.get("elevation", 0.0)))
					colors.append(_tile_color(sample))
					water_samples.append(1.0 if _is_water(sample) else 0.0)
			if samples.is_empty():
				continue
			corner_heights[corner] = _average_float(samples)
			corner_colors[corner] = _average_color(colors)
			corner_water[corner] = _average_float(water_samples)

func _average_float(values: Array) -> float:
	var total := 0.0
	for value in values:
		total += float(value)
	return total / max(1, values.size())

func _average_color(values: Array) -> Color:
	var result := Color(0, 0, 0, 1)
	for value in values:
		result += value
	return result / float(max(1, values.size()))

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
	var z := func(key: Vector2i) -> float: return corner_heights.get(key, elevation)
	return PackedVector2Array([
		center + Vector2(0, -CELL_H * 0.5 - z.call(Vector2i(x, y)) * HEIGHT_SCALE) * scale,
		center + Vector2(CELL_W * 0.5, -z.call(Vector2i(x + 1, y)) * HEIGHT_SCALE) * scale,
		center + Vector2(0, CELL_H * 0.5 - z.call(Vector2i(x + 1, y + 1)) * HEIGHT_SCALE) * scale,
		center + Vector2(-CELL_W * 0.5, -z.call(Vector2i(x, y + 1)) * HEIGHT_SCALE) * scale
	])

func _rebuild_terrain_mesh() -> void:
	if not map_ready:
		return
	var vertices := PackedVector2Array()
	var colors := PackedColorArray()
	var uvs := PackedVector2Array()
	var indices := PackedInt32Array()
	var vertex_lookup := {}
	for tile in tiles:
		var x := int(tile.get("x", 0))
		var y := int(tile.get("y", 0))
		var keys := [Vector2i(x, y), Vector2i(x + 1, y), Vector2i(x + 1, y + 1), Vector2i(x, y + 1)]
		var tile_indices: Array = []
		for key in keys:
			if not vertex_lookup.has(key):
				vertex_lookup[key] = vertices.size()
				var corner_tile: Dictionary = tile_index.get(key, tile)
				vertices.append(_corner_screen_point(key, float(corner_heights.get(key, tile.get("elevation", 0.0)))))
				colors.append(corner_colors.get(key, _tile_color(corner_tile)))
				uvs.append(Vector2(float(corner_water.get(key, 1.0 if _is_water(corner_tile) else 0.0)), clamp(float(corner_heights.get(key, 0.0)), 0.0, 1.0)))
			tile_indices.append(int(vertex_lookup[key]))
		indices.append_array([tile_indices[0], tile_indices[1], tile_indices[2], tile_indices[0], tile_indices[2], tile_indices[3]])
	var arrays := []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = vertices
	arrays[Mesh.ARRAY_COLOR] = colors
	arrays[Mesh.ARRAY_TEX_UV] = uvs
	arrays[Mesh.ARRAY_INDEX] = indices
	terrain_mesh.clear_surfaces()
	terrain_mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	terrain_mesh_instance.mesh = terrain_mesh

func _corner_screen_point(key: Vector2i, elevation: float) -> Vector2:
	var x := float(key.x)
	var y := float(key.y)
	return camera + Vector2((x - y) * CELL_W * 0.5, (x + y) * CELL_H * 0.5 - elevation * HEIGHT_SCALE) * zoom

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
	for route in routes:
		_draw_route(route)
	_draw_cities()

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
			_rebuild_terrain_mesh()
		elif event.button_index == MOUSE_BUTTON_WHEEL_DOWN:
			zoom = clamp(zoom / 1.1, 0.35, 3.0)
			_rebuild_terrain_mesh()
		elif event.button_index == MOUSE_BUTTON_MIDDLE or (event.button_index == MOUSE_BUTTON_LEFT and event.shift_pressed):
			dragging = event.pressed
			drag_last = event.position
	elif event is InputEventMouseMotion and dragging:
		camera += event.position - drag_last
		drag_last = event.position
		_rebuild_terrain_mesh()
		queue_redraw()

func _on_viewport_resized() -> void:
	if map_ready:
		_fit_map()
		_rebuild_terrain_mesh()
	queue_redraw()
