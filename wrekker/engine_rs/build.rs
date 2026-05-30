use std::process::Command;

fn main() {
    println!("cargo:rustc-check-cfg=cfg(wrekker_has_rubberband)");

    let feature_enabled = std::env::var_os("CARGO_FEATURE_RUBBERBAND").is_some();
    if !feature_enabled {
        return;
    }

    let output = Command::new("pkg-config")
        .args(["--libs", "rubberband"])
        .output();

    let Ok(output) = output else {
        println!(
            "cargo:warning=pkg-config not found; Rubber Band wrapper will use passthrough fallback"
        );
        return;
    };

    if !output.status.success() {
        println!("cargo:warning=librubberband not found by pkg-config; Rubber Band wrapper will use passthrough fallback");
        return;
    }

    let libs = String::from_utf8_lossy(&output.stdout);
    for token in libs.split_whitespace() {
        if let Some(path) = token.strip_prefix("-L") {
            println!("cargo:rustc-link-search=native={path}");
        } else if let Some(lib) = token.strip_prefix("-l") {
            println!("cargo:rustc-link-lib={lib}");
        }
    }

    println!("cargo:rustc-cfg=wrekker_has_rubberband");
}
