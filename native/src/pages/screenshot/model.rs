use crate::state::{Page, State};
use rune::{Drag, Key, KeyEvent, Phase};
use serde_json::{Value, json};

pub const TOOLS: &[(&str, &str)] = &[
    ("select", "Crop"),
    ("window", "Window"),
    ("move", "Move mark"),
    ("arrow", "Arrow"),
    ("rect", "Box"),
    ("freehand", "Pen"),
    ("highlight", "Highlight"),
    ("text", "Text"),
    ("step", "Step"),
];
pub const SWATCHES: &[(&str, &str)] = &[
    ("Coral", "#FF4F3A"),
    ("Amber", "#F5A524"),
    ("Blue", "#3A7BFD"),
    ("Green", "#1FA463"),
];

pub fn n(v: &Value, key: &str, default: f32) -> f32 {
    v[key].as_f64().map(|v| v as f32).unwrap_or(default)
}
pub fn text<'a>(v: &'a Value, key: &str, default: &'a str) -> &'a str {
    v[key].as_str().unwrap_or(default)
}
pub fn dimensions(s: &State) -> [f32; 2] {
    let r = &s.data["screenshot.choose"];
    [n(r, "width", 1.).max(1.), n(r, "height", 1.).max(1.)]
}
pub fn fit(source: [f32; 2], viewport: [f32; 2]) -> [f32; 3] {
    let scale = (viewport[0] / source[0])
        .min(viewport[1] / source[1])
        .max(0.0001);
    [
        (viewport[0] - source[0] * scale) / 2.,
        (viewport[1] - source[1] * scale) / 2.,
        scale,
    ]
}
pub fn point(source: [f32; 2], viewport: [f32; 2], x: f32, y: f32) -> [f32; 2] {
    let [ox, oy, scale] = fit(source, viewport);
    [
        ((x - ox) / scale).clamp(0., source[0]),
        ((y - oy) / scale).clamp(0., source[1]),
    ]
}
pub fn rect(a: [f32; 2], b: [f32; 2]) -> Value {
    json!({"x":a[0].min(b[0]).round(),"y":a[1].min(b[1]).round(),"width":(a[0]-b[0]).abs().round(),"height":(a[1]-b[1]).abs().round()})
}
pub fn begin_screenshot(s: &mut State, request: &Value) {
    let previous = if s.page == Page::Screenshot {
        s.data["screenshot.return_page"]
            .as_str()
            .unwrap_or("walkthrough")
            .to_string()
    } else {
        s.page.key().to_string()
    };
    s.data["screenshot.return_page"] = json!(previous);
    s.data["screenshot.choose"] = request.clone();
    let mode = text(request, "mode", "region");
    let has_windows = request["windows"]
        .as_array()
        .is_some_and(|windows| !windows.is_empty());
    let [w, h] = dimensions(s);
    s.data["screenshot.editor"] = json!({"tool":if mode=="screen"{"arrow"}else if mode=="window" && has_windows{"window"}else{"select"},
        "color":"#FF4F3A","width":4.,"zoom":1.,"pan":[0.,0.],"annotations":[],"undo":[],"redo":[],"selected":null,
        "region":if mode=="screen"{json!({"x":0,"y":0,"width":w,"height":h})}else{Value::Null}});
    s.edit("screenshot-label").text.clear();
    s.error = None;
    s.notice = None;
    s.page = Page::Screenshot;
    s.report_window_focus();
    if let Some(path) = request["image_path"].as_str() {
        s.load_image(path);
    }
}

pub fn finish_screenshot(s: &mut State, cancelled: bool) {
    if s.pending {
        return;
    }
    let request = &s.data["screenshot.choose"];
    if request["id"].is_null() {
        return;
    }
    let editor = &s.data["screenshot.editor"];
    if !cancelled && !valid_region(&editor["region"]) {
        s.error = Some("Draw a region or choose a window before keeping this screenshot.".into());
        return;
    }
    s.command("screenshot.complete",json!({"id":request["id"],"cancelled":cancelled,"region":editor["region"],"annotations":editor["annotations"]}));
}

pub fn screenshot_finished(s: &mut State) {
    s.page = match s.data["screenshot.return_page"]
        .as_str()
        .unwrap_or("walkthrough")
    {
        "record" => Page::Record,
        "files" => Page::Files,
        "meetings" => Page::Meetings,
        "history" => Page::History,
        "settings" => Page::Settings,
        "scratchpad" => Page::Scratchpad,
        _ => Page::Walkthrough,
    };
    s.report_window_focus();
    s.data["screenshot.editor"] = Value::Null;
    s.data["screenshot.choose"] = Value::Null;
    if s.page == Page::Walkthrough {
        super::super::capture::reload_walkthrough_gallery(s);
    }
    for (key, edit) in &mut s.edits {
        if key.starts_with("screenshot-") {
            edit.focused = false;
        }
    }
}

pub fn valid_region(region: &Value) -> bool {
    n(region, "width", 0.) >= 2. && n(region, "height", 0.) >= 2.
}
fn snapshot(s: &State) -> Value {
    let e = &s.data["screenshot.editor"];
    json!({"region":e["region"],"annotations":e["annotations"]})
}
pub fn remember(s: &mut State) {
    let snapshot = snapshot(s);
    let e = &mut s.data["screenshot.editor"];
    if let Some(undo) = e["undo"].as_array_mut() {
        if undo.len() >= 100 {
            undo.remove(0);
        }
        undo.push(snapshot);
    }
    e["redo"] = json!([]);
}
pub fn undo(s: &mut State, redo: bool) {
    let now = snapshot(s);
    let e = &mut s.data["screenshot.editor"];
    let key = if redo { "redo" } else { "undo" };
    let Some(previous) = e[key].as_array_mut().and_then(Vec::pop) else {
        return;
    };
    e[if redo { "undo" } else { "redo" }]
        .as_array_mut()
        .unwrap()
        .push(now);
    e["region"] = previous["region"].clone();
    e["annotations"] = previous["annotations"].clone();
    e["draft"] = Value::Null;
    e["selected"] = Value::Null;
}
pub fn choose_tool(s: &mut State, tool: &str) {
    s.data["screenshot.editor"]["tool"] = json!(tool);
    s.data["screenshot.editor"]["draft"] = Value::Null;
    s.edit("screenshot-label").focused = false;
}
fn window_at(s: &State, p: [f32; 2]) -> Option<Value> {
    s.data["screenshot.choose"]["windows"]
        .as_array()?
        .iter()
        .find(|window| {
            p[0] >= n(window, "x", 0.)
                && p[1] >= n(window, "y", 0.)
                && p[0] <= n(window, "x", 0.) + n(window, "width", 0.)
                && p[1] <= n(window, "y", 0.) + n(window, "height", 0.)
        })
        .cloned()
}
pub fn choose_window(s: &mut State, window: &Value) {
    remember(s);
    let [w, h] = dimensions(s);
    let x = n(window, "x", 0.).clamp(0., w);
    let y = n(window, "y", 0.).clamp(0., h);
    let right = (n(window, "x", 0.) + n(window, "width", 0.)).clamp(x, w);
    let bottom = (n(window, "y", 0.) + n(window, "height", 0.)).clamp(y, h);
    s.data["screenshot.editor"]["region"] = json!({"x":x,"y":y,"width":right-x,"height":bottom-y});
    s.data["screenshot.editor"]["tool"] = json!("arrow");
}
pub fn hover(s: &mut State, x: f32, y: f32, viewport: [f32; 2]) {
    if s.data["screenshot.editor"]["tool"] != "window" {
        return;
    }
    s.data["screenshot.editor"]["hover_window"] =
        window_at(s, point(dimensions(s), viewport, x, y)).unwrap_or(Value::Null);
}
fn pts(annotation: &Value) -> Vec<[f32; 2]> {
    annotation["points"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|p| [n(p, "x", 0.), n(p, "y", 0.)])
        .collect()
}
fn hit_annotation(s: &State, p: [f32; 2]) -> Option<usize> {
    s.data["screenshot.editor"]["annotations"]
        .as_array()?
        .iter()
        .enumerate()
        .rev()
        .find_map(|(index, a)| {
            let points = pts(a);
            let first = points.first()?;
            let padding = 18. * dimensions(s)[0] / 1200.;
            let (mut min, mut max) = (*first, *first);
            for point in &points {
                for axis in 0..2 {
                    min[axis] = min[axis].min(point[axis]);
                    max[axis] = max[axis].max(point[axis]);
                }
            }
            if text(a, "tool", "") == "text" {
                max[0] += text(a, "text", "").chars().count() as f32 * n(a, "font_size", 24.) * 0.6;
                max[1] += n(a, "font_size", 24.);
            }
            (p[0] >= min[0] - padding
                && p[0] <= max[0] + padding
                && p[1] >= min[1] - padding
                && p[1] <= max[1] + padding)
                .then_some(index)
        })
}
pub fn delete_selected(s: &mut State) {
    let Some(index) = s.data["screenshot.editor"]["selected"]
        .as_u64()
        .map(|v| v as usize)
    else {
        return;
    };
    remember(s);
    if let Some(annotations) = s.data["screenshot.editor"]["annotations"].as_array_mut() {
        if index < annotations.len() {
            annotations.remove(index);
        }
    }
    s.data["screenshot.editor"]["selected"] = Value::Null;
}

pub fn precision(s: &mut State) {
    let region = s.data["screenshot.editor"]["region"].clone();
    for key in ["x", "y", "width", "height"] {
        s.edit(&format!("screenshot-crop-{key}")).text = format!("{}", n(&region, key, 0.) as u32);
    }
    s.data["screenshot.editor"]["precision"] = json!(true);
}
pub fn apply_precision(s: &mut State) {
    let mut numbers = vec![];
    for key in ["x", "y", "width", "height"] {
        match s
            .edit(&format!("screenshot-crop-{key}"))
            .text
            .trim()
            .parse::<f32>()
        {
            Ok(value) if value.is_finite() && value >= 0. => numbers.push(value.round()),
            _ => {
                s.error = Some("Use whole, positive pixel values for the crop.".into());
                return;
            }
        }
    }
    let [w, h] = dimensions(s);
    let x = numbers[0].min(w);
    let y = numbers[1].min(h);
    let region = json!({"x":x,"y":y,"width":numbers[2].min(w-x),"height":numbers[3].min(h-y)});
    if !valid_region(&region) {
        s.error = Some("Choose a crop at least 2 × 2 pixels inside the capture.".into());
        return;
    }
    remember(s);
    s.data["screenshot.editor"]["region"] = region;
    s.data["screenshot.editor"]["precision"] = json!(false);
    choose_tool(s, "arrow");
}

fn crop_anchor(s: &State, p: [f32; 2], viewport: [f32; 2]) -> Option<[f32; 2]> {
    let r = &s.data["screenshot.editor"]["region"];
    if !valid_region(r) {
        return None;
    }
    let (x, y, w, h) = (
        n(r, "x", 0.),
        n(r, "y", 0.),
        n(r, "width", 0.),
        n(r, "height", 0.),
    );
    let radius = 9. / fit(dimensions(s), viewport)[2];
    for (corner, opposite) in [
        ([x, y], [x + w, y + h]),
        ([x + w, y], [x, y + h]),
        ([x, y + h], [x + w, y]),
        ([x + w, y + h], [x, y]),
    ] {
        if (corner[0] - p[0]).hypot(corner[1] - p[1]) <= radius {
            return Some(opposite);
        }
    }
    None
}

pub fn drag(s: &mut State, d: &Drag) {
    let source = dimensions(s);
    let viewport = [d.w, d.h];
    let current = point(source, viewport, d.x, d.y);
    let start = point(source, viewport, d.start_x, d.start_y);
    let mut tool = text(&s.data["screenshot.editor"], "tool", "select").to_string();
    if d.phase == Phase::Down {
        s.data["screenshot.editor"]["drag_tool"] = Value::Null;
        if !["move", "pan", "window"].contains(&tool.as_str()) {
            if let Some(anchor) = crop_anchor(s, start, viewport) {
                s.data["screenshot.editor"]["resize_anchor"] = json!(anchor);
                s.data["screenshot.editor"]["drag_tool"] = json!("resize");
                tool = "resize".into();
            }
        }
    } else if let Some(active) = s.data["screenshot.editor"]["drag_tool"].as_str() {
        tool = active.to_string();
    }
    if d.phase == Phase::Down {
        s.data["screenshot.editor"]["dragging"] = json!(true);
        s.data["screenshot.editor"]["before_drag"] = snapshot(s);
        s.data["screenshot.editor"]["pan_before"] = s.data["screenshot.editor"]["pan"].clone();
        if tool == "move" {
            let selected = hit_annotation(s, start);
            s.data["screenshot.editor"]["selected"] = json!(selected);
            s.data["screenshot.editor"]["mark_before"] = selected
                .map(|i| s.data["screenshot.editor"]["annotations"][i].clone())
                .unwrap_or(Value::Null);
            if let Some(index) = selected {
                if s.data["screenshot.editor"]["annotations"][index]["tool"] == "text" {
                    s.edit("screenshot-label").text = s.data["screenshot.editor"]["annotations"]
                        [index]["text"]
                        .as_str()
                        .unwrap_or("")
                        .into();
                }
            }
        } else if !["select", "resize", "window", "pan"].contains(&tool.as_str()) {
            let base = 1. / fit(source, viewport)[2];
            let e = &s.data["screenshot.editor"];
            let step = e["annotations"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|a| a["tool"] == "step")
                .count()
                + 1;
            let content = if tool == "step" {
                step.to_string()
            } else {
                s.edits
                    .get("screenshot-label")
                    .map(|e| e.text.clone())
                    .unwrap_or_default()
            };
            let points = if ["text", "step"].contains(&tool.as_str()) {
                json!([{"x":start[0],"y":start[1]}])
            } else {
                json!([{"x":start[0],"y":start[1]},{"x":start[0],"y":start[1]}])
            };
            s.data["screenshot.editor"]["draft"] = json!({"tool":tool,"color":text(e,"color","#FF4F3A"),"width":(n(e,"width",4.)*base*if tool=="highlight"{4.5}else{1.}).clamp(1.,200.),"font_size":(if tool=="step"{if step>=100{11.}else{14.}}else{16.})*base,"radius":14.*base,"text":content,"points":points});
        }
    }
    if d.cancelled() {
        s.data["screenshot.editor"]["dragging"] = json!(false);
        let before = s.data["screenshot.editor"]["before_drag"].clone();
        s.data["screenshot.editor"]["region"] = before["region"].clone();
        s.data["screenshot.editor"]["annotations"] = before["annotations"].clone();
        s.data["screenshot.editor"]["draft"] = Value::Null;
        return;
    }
    match tool.as_str() {
        "select" => {
            s.data["screenshot.editor"]["region"] = rect(start, current);
        }
        "resize" => {
            let anchor = &s.data["screenshot.editor"]["resize_anchor"];
            s.data["screenshot.editor"]["region"] = rect(
                [
                    anchor[0].as_f64().unwrap_or(0.) as f32,
                    anchor[1].as_f64().unwrap_or(0.) as f32,
                ],
                current,
            );
        }
        "window" => {
            if d.phase == Phase::Up {
                if let Some(window) = window_at(s, current) {
                    choose_window(s, &window);
                }
            }
        }
        "pan" => {
            let zoom = n(&s.data["screenshot.editor"], "zoom", 1.);
            let before = s.data["screenshot.editor"]["pan_before"].clone();
            let max = [d.w * (zoom - 1.) / 2., d.h * (zoom - 1.) / 2.];
            s.data["screenshot.editor"]["pan"] = json!([
                (before[0].as_f64().unwrap_or(0.) as f32 + d.dx).clamp(-max[0], max[0]),
                (before[1].as_f64().unwrap_or(0.) as f32 + d.dy).clamp(-max[1], max[1])
            ]);
        }
        "move" => {
            if let Some(index) = s.data["screenshot.editor"]["selected"]
                .as_u64()
                .map(|i| i as usize)
            {
                let original = s.data["screenshot.editor"]["mark_before"].clone();
                let points = pts(&original);
                let min = [
                    points.iter().map(|p| p[0]).fold(f32::INFINITY, f32::min),
                    points.iter().map(|p| p[1]).fold(f32::INFINITY, f32::min),
                ];
                let max = [
                    points
                        .iter()
                        .map(|p| p[0])
                        .fold(f32::NEG_INFINITY, f32::max),
                    points
                        .iter()
                        .map(|p| p[1])
                        .fold(f32::NEG_INFINITY, f32::max),
                ];
                let delta = [
                    (current[0] - start[0]).clamp(-min[0], source[0] - max[0]),
                    (current[1] - start[1]).clamp(-min[1], source[1] - max[1]),
                ];
                let translated: Vec<Value> = points
                    .iter()
                    .map(|p| json!({"x":p[0]+delta[0],"y":p[1]+delta[1]}))
                    .collect();
                s.data["screenshot.editor"]["annotations"][index]["points"] = json!(translated);
            }
        }
        _ => {
            let draft = &mut s.data["screenshot.editor"]["draft"];
            if let Some(points) = draft["points"].as_array_mut() {
                if ["freehand", "highlight"].contains(&tool.as_str()) {
                    if points.len() < 10000 {
                        points.push(json!({"x":current[0],"y":current[1]}));
                    }
                } else if points.len() > 1 {
                    points[1] = json!({"x":current[0],"y":current[1]});
                }
            }
        }
    }
    if d.phase == Phase::Up {
        s.data["screenshot.editor"]["dragging"] = json!(false);
        s.data["screenshot.editor"]["drag_tool"] = Value::Null;
    }
    if d.phase != Phase::Up || ["pan", "window"].contains(&tool.as_str()) {
        return;
    }
    let before = s.data["screenshot.editor"]["before_drag"].clone();
    let e = &mut s.data["screenshot.editor"];
    if ["select", "resize"].contains(&tool.as_str()) && !valid_region(&e["region"]) {
        e["region"] = before["region"].clone();
        return;
    }
    if !["select", "resize", "move"].contains(&tool.as_str()) {
        let draft = e["draft"].clone();
        if ["arrow", "rect", "ellipse"].contains(&tool.as_str())
            && (current[0] - start[0]).hypot(current[1] - start[1])
                < 6. * (source[0] / 1200.).max(0.5)
        {
            e["draft"] = Value::Null;
            return;
        }
        if tool == "text" && text(&draft, "text", "").trim().is_empty() {
            e["draft"] = Value::Null;
            s.notice = Some("Type a label, then click where it belongs.".into());
            return;
        }
        if let Some(annotations) = e["annotations"].as_array_mut() {
            if annotations.len() < 500 {
                annotations.push(draft);
            }
        }
        e["draft"] = Value::Null;
    }
    if before["region"] == e["region"] && before["annotations"] == e["annotations"] {
        return;
    }
    if let Some(undo) = e["undo"].as_array_mut() {
        if undo.len() >= 100 {
            undo.remove(0);
        }
        undo.push(before);
    }
    e["redo"] = json!([]);
    if tool == "select" {
        e["tool"] = json!("arrow");
    }
}

pub fn screenshot_key(s: &mut State, event: &KeyEvent) -> bool {
    if s.page != Page::Screenshot {
        return false;
    }
    if event.key == Key::Escape {
        finish_screenshot(s, true);
        return true;
    }
    if s.edits
        .iter()
        .any(|(key, edit)| key.starts_with("screenshot-") && edit.focused)
    {
        return false;
    }
    if event.is_command('z') {
        undo(s, event.mods.shift);
        return true;
    }
    match &event.key {
        Key::Enter => {
            finish_screenshot(s, false);
            true
        }
        Key::Backspace | Key::Delete => {
            delete_selected(s);
            true
        }
        Key::Text(key) => {
            let tool = match key.to_lowercase().as_str() {
                "a" => "arrow",
                "b" => "rect",
                "p" => "freehand",
                "h" => "highlight",
                "t" => "text",
                "n" => "step",
                "c" => "select",
                "v" => "move",
                " " => "pan",
                _ => "",
            };
            if !tool.is_empty() {
                choose_tool(s, tool);
                return true;
            }
            if let Ok(i) = key.parse::<usize>() {
                if (1..=SWATCHES.len()).contains(&i) {
                    s.data["screenshot.editor"]["color"] = json!(SWATCHES[i - 1].1);
                    return true;
                }
            }
            false
        }
        _ => false,
    }
}
