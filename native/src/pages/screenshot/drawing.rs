use super::model::{dimensions, fit, n, text, valid_region};
use crate::state::State;
use crate::ui::{Palette, label};
use rune::{Color, El, LineCap, Shape, canvas, el};
use serde_json::Value;

fn polygon(x: f32, y: f32, w: f32, h: f32) -> Vec<[f32; 2]> {
    vec![[x, y], [x + w, y], [x + w, y + h], [x, y + h]]
}
fn points(annotation: &Value, transform: [f32; 3]) -> Vec<[f32; 2]> {
    annotation["points"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|p| {
            [
                transform[0] + n(p, "x", 0.) * transform[2],
                transform[1] + n(p, "y", 0.) * transform[2],
            ]
        })
        .collect()
}

pub fn overlay(s: &State, p: &Palette, viewport: [f32; 2]) -> El {
    let source = dimensions(s);
    let transform = fit(source, viewport);
    let [ox, oy, scale] = transform;
    let e = &s.data["screenshot.editor"];
    let mut shape = Shape::new();
    let mut labels = vec![];
    let region = if e["tool"] == "window" && e["hover_window"].is_object() {
        &e["hover_window"]
    } else {
        &e["region"]
    };
    if valid_region(region) {
        let x = ox + n(region, "x", 0.) * scale;
        let y = oy + n(region, "y", 0.) * scale;
        let w = n(region, "width", 0.) * scale;
        let h = n(region, "height", 0.) * scale;
        let dim = Color::BLACK.alpha(0.42);
        for [rx, ry, rw, rh] in [
            [ox, oy, source[0] * scale, (y - oy).max(0.)],
            [ox, y, (x - ox).max(0.), h],
            [x + w, y, (ox + source[0] * scale - x - w).max(0.), h],
            [
                ox,
                y + h,
                source[0] * scale,
                (oy + source[1] * scale - y - h).max(0.),
            ],
        ] {
            if rw > 0. && rh > 0. {
                shape = shape.fill(polygon(rx, ry, rw, rh), dim);
            }
        }
        shape = shape
            .stroke_with(polygon(x, y, w, h), true, 1.5, LineCap::Butt, Color::WHITE)
            .stroke_with(
                polygon(x - 1.5, y - 1.5, w + 3., h + 3.),
                true,
                1.,
                LineCap::Butt,
                p.accent,
            );
        for [cx, cy] in [[x, y], [x + w, y], [x, y + h], [x + w, y + h]] {
            shape = shape.fill(polygon(cx - 3., cy - 3., 6., 6.), Color::WHITE);
        }
    }
    let mut all = e["annotations"].as_array().cloned().unwrap_or_default();
    if e["draft"].is_object() {
        all.push(e["draft"].clone());
    }
    for (index, a) in all.iter().enumerate() {
        let points = points(a, transform);
        let Some(&first) = points.first() else {
            continue;
        };
        let end = *points.last().unwrap();
        let color = rune::color::hex(text(a, "color", "#FF4F3A")).unwrap_or(p.accent);
        let width = (n(a, "width", 4.) * scale).max(0.7);
        match text(a, "tool", "") {
            "arrow" => {
                shape = shape.line(first, end, width, LineCap::Round, color);
                let angle = (end[1] - first[1]).atan2(end[0] - first[0]);
                let length = (width * 4.).max(10. * scale);
                let a = [
                    end[0] - length * (angle - 0.5).cos(),
                    end[1] - length * (angle - 0.5).sin(),
                ];
                let b = [
                    end[0] - length * (angle + 0.5).cos(),
                    end[1] - length * (angle + 0.5).sin(),
                ];
                shape = shape.fill(vec![end, a, b], color);
            }
            "rect" => {
                let x = first[0].min(end[0]);
                let y = first[1].min(end[1]);
                let w = (first[0] - end[0]).abs();
                let h = (first[1] - end[1]).abs();
                shape = shape
                    .fill(polygon(x, y, w, h), color.alpha(0.09))
                    .stroke_with(polygon(x, y, w, h), true, width, LineCap::Round, color);
            }
            "ellipse" => {
                let center = [(first[0] + end[0]) / 2., (first[1] + end[1]) / 2.];
                let radius = [
                    (first[0] - end[0]).abs() / 2.,
                    (first[1] - end[1]).abs() / 2.,
                ];
                let circle: Vec<_> = (0..48)
                    .map(|i| {
                        let angle = i as f32 * std::f32::consts::TAU / 48.;
                        [
                            center[0] + angle.cos() * radius[0],
                            center[1] + angle.sin() * radius[1],
                        ]
                    })
                    .collect();
                shape = shape.stroke_with(circle, true, width, LineCap::Round, color);
            }
            "freehand" => {
                shape = shape.stroke(points.clone(), width, color);
            }
            "highlight" => {
                shape = shape.stroke(points.clone(), width, color.alpha(0.38));
            }
            "text" => {
                let size = (n(a, "font_size", 24.) * scale).max(4.);
                labels.push(
                    label(text(a, "text", ""), size, 650., Color::WHITE)
                        .padding(size * 0.25)
                        .radius(size * 0.2)
                        .fill(color)
                        .absolute(first[0], first[1])
                        .inert(),
                );
            }
            "step" => {
                let radius = (n(a, "radius", 14.) * scale).max(5.);
                let circle: Vec<_> = (0..40)
                    .map(|i| {
                        let angle = i as f32 * std::f32::consts::TAU / 40.;
                        [
                            first[0] + angle.cos() * radius,
                            first[1] + angle.sin() * radius,
                        ]
                    })
                    .collect();
                shape = shape.fill(circle, color);
                labels.push(
                    el().size(radius * 2., radius * 2.)
                        .center()
                        .absolute(first[0] - radius, first[1] - radius)
                        .inert()
                        .child(label(
                            text(a, "text", "1"),
                            (n(a, "font_size", 14.) * scale).max(4.),
                            700.,
                            if color.luminance() > 0.3 {
                                Color::BLACK
                            } else {
                                Color::WHITE
                            },
                        )),
                );
            }
            _ => {}
        }
        if e["selected"].as_u64() == Some(index as u64) {
            let min = [
                points.iter().map(|v| v[0]).fold(f32::INFINITY, f32::min),
                points.iter().map(|v| v[1]).fold(f32::INFINITY, f32::min),
            ];
            let max = [
                points
                    .iter()
                    .map(|v| v[0])
                    .fold(f32::NEG_INFINITY, f32::max),
                points
                    .iter()
                    .map(|v| v[1])
                    .fold(f32::NEG_INFINITY, f32::max),
            ];
            shape = shape.stroke_with(
                polygon(
                    min[0] - 6.,
                    min[1] - 6.,
                    (max[0] - min[0]).max(12.) + 12.,
                    (max[1] - min[1]).max(12.) + 12.,
                ),
                true,
                1.,
                LineCap::Butt,
                p.accent,
            );
        }
    }
    let mut overlay = canvas(shape)
        .absolute(0., 0.)
        .size(viewport[0], viewport[1])
        .inert();
    for node in labels {
        overlay = overlay.child(node);
    }
    overlay
}
