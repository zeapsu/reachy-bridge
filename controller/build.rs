//! The binary embeds `web/dist`. Create an empty one when the UI hasn't been
//! built, so `cargo build`/`cargo test` work on their own; the controller then
//! answers `/` with a hint to build the UI.

fn main() {
    let dist = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../web/dist");
    if !dist.exists() {
        std::fs::create_dir_all(&dist).expect("create web/dist");
        println!("cargo::warning=web/dist is missing; building without the dashboard (run `bun run build` in web/)");
    }
    println!("cargo::rerun-if-changed=../web/dist");
}
