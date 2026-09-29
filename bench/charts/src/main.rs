//! Charts for BENCHMARKS.md, rendered with charton from bench/results/*.json.

use charton::prelude::*;
use serde_json::Value;
use std::error::Error;
use std::path::{Path, PathBuf};

type R<T> = Result<T, Box<dyn Error>>;

const SHORT: &[(&str, &str)] = &[
    ("this-repo", "this repo (Rust)"),
    ("nabid", "nabid-pf (Node)"),
    ("anaisbetts", "anaisbetts (Node+yt-dlp)"),
    ("spinalshock-norl", "spinalshock (Go+yt-dlp, no sleep)"),
    ("spinalshock", "spinalshock (Go+yt-dlp, shipped)"),
];

fn short(key: &str) -> String {
    SHORT.iter().find(|(k, _)| *k == key).map_or(key, |(_, v)| v).to_string()
}

fn results_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../results")
}

fn load(name: &str) -> R<Option<Value>> {
    let p = results_dir().join(name);
    if !p.exists() {
        return Ok(None);
    }
    Ok(Some(serde_json::from_str(&std::fs::read_to_string(p)?)?))
}

fn out(name: &str) -> String {
    results_dir().join(name).to_string_lossy().into_owned()
}

fn profile<'a>(d: &'a Value, name: &str) -> R<&'a Value> {
    d["profiles"]
        .as_array()
        .and_then(|ps| ps.iter().find(|p| p["profile"] == name))
        .ok_or_else(|| format!("profile {name} missing").into())
}

/// (label, summary) for every row that ran successfully.
fn rows(p: &Value) -> Vec<(String, &Value)> {
    p["rows"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|r| r.get("summary").is_some())
        .map(|r| (short(r["key"].as_str().unwrap_or("?")), &r["summary"]))
        .collect()
}

fn bar(labels: Vec<String>, values: Vec<f64>, y_label: &str, title: &str, file: &str) -> R<()> {
    let implementation = labels;
    let value = values;
    chart!(implementation, value)?
        .mark_bar()?
        .encode((alt::x("implementation"), alt::y("value").with_zero(true)))?
        .coord_flip()
        .with_size(760, 340)
        .with_title(title)
        .with_x_label("")
        .with_y_label(y_label)
        .save(out(file))?;
    println!("wrote bench/results/{file}");
    Ok(())
}

fn mock_charts(d: &Value) -> R<()> {
    let n = d["params"]["rounds"].as_u64().unwrap_or(0) * d["params"]["iters"].as_u64().unwrap_or(0);
    let prod = profile(d, "PROD-sim")?;
    let lan = profile(d, "LAN")?;
    let rp = rows(prod);

    bar(
        rp.iter().map(|(l, _)| l.clone()).collect(),
        rp.iter().map(|(_, s)| s["p50_ms"].as_f64().unwrap_or(f64::NAN)).collect(),
        "p50 latency per request (ms)",
        &format!("p50 latency, 80 ms simulated RTT (n={n})"),
        "chart-latency-prod.svg",
    )?;
    bar(
        rp.iter().map(|(l, _)| l.clone()).collect(),
        rp.iter().map(|(_, s)| s["rss_mib_max"].as_f64().unwrap_or(f64::NAN)).collect(),
        "peak RSS (MiB)",
        &format!("Peak server memory after {n} calls"),
        "chart-memory.svg",
    )?;
    let rl = rows(lan);
    bar(
        rl.iter().map(|(l, _)| l.clone()).collect(),
        rl.iter().map(|(_, s)| s["cold_ms_median"].as_f64().unwrap_or(f64::NAN)).collect(),
        "spawn → first transcript (ms, median of 5)",
        "Cold start, loopback mock",
        "chart-cold-start.svg",
    )?;

    // X/Y: latency vs memory, one point per server (PROD-sim).
    let latency_ms: Vec<f64> = rp.iter().map(|(_, s)| s["p50_ms"].as_f64().unwrap_or(f64::NAN)).collect();
    let rss_mib: Vec<f64> = rp.iter().map(|(_, s)| s["rss_mib_max"].as_f64().unwrap_or(f64::NAN)).collect();
    let server: Vec<String> = rp.iter().map(|(l, _)| l.clone()).collect();
    let points = chart!(&latency_ms, &rss_mib, &server)?
        .mark_point()?
        .configure_point(|p| p.with_size(7.0))
        .encode((
            alt::x("latency_ms").with_zero(true),
            alt::y("rss_mib").with_zero(true),
            alt::color("server"),
        ))?;
    points
        .with_size(760, 460)
        .with_title("Latency vs memory, 80 ms simulated RTT (lower-left is better)")
        .with_x_label("p50 latency per request (ms)")
        .with_y_label("peak RSS (MiB)")
                .save(out("chart-latency-vs-memory.svg"))?;
    println!("wrote bench/results/chart-latency-vs-memory.svg");

    // Per-request latency distribution on loopback (as-shipped spinalshock
    // omitted: its 1.5–3 s sleep would flatten every other box).
    let mut implementation = Vec::new();
    let mut latency_ms = Vec::new();
    for r in lan["rows"].as_array().into_iter().flatten() {
        let key = r["key"].as_str().unwrap_or("?");
        if key == "spinalshock" || r.get("samples_ms").is_none() {
            continue;
        }
        for s in r["samples_ms"].as_array().into_iter().flatten() {
            implementation.push(short(key));
            latency_ms.push(s.as_f64().unwrap_or(f64::NAN));
        }
    }
    chart!(implementation, latency_ms)?
        .mark_boxplot()?
        .encode((alt::x("implementation"), alt::y("latency_ms")))?
        .coord_flip()
        .with_size(760, 340)
        .with_title(format!("Per-request latency, loopback mock (n={n} each)"))
        .with_x_label("")
        .with_y_label("latency per request (ms)")
        .save(out("chart-latency-distribution-lan.svg"))?;
    println!("wrote bench/results/chart-latency-distribution-lan.svg");
    Ok(())
}

fn micro_charts(d: &Value) -> R<()> {
    let iters = d["params"]["parse_iters"].as_f64().unwrap_or(1.0);
    let names = [("rust", "Rust core (quick-xml)"), ("ts", "TS port (fast-xml-parser)"), ("python", "jdepoix (defusedxml)")];
    let mut labels = Vec::new();
    let mut values = Vec::new();
    for (k, label) in names {
        if let Some(ms) = d["rows"][k]["parse_xml"]["ms"].as_f64() {
            labels.push(label.to_string());
            values.push(ms * 1000.0 / iters);
        }
    }
    bar(
        labels,
        values,
        "µs per parse",
        "Caption XML parse, 80 KiB / 1000 cues",
        "chart-xml-parse.svg",
    )
}

fn main() -> R<()> {
    if let Some(d) = load("mock.json")? {
        mock_charts(&d)?;
    }
    if let Some(d) = load("micro.json")? {
        micro_charts(&d)?;
    }
    Ok(())
}
