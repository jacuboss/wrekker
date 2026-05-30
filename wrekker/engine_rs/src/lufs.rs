//! ITU-R BS.1770-4 K-weighted LUFS metering.
//!
//! Two biquad stages per channel (pre-filter + RLB high-pass), then a sliding
//! mean-square ring buffer for momentary (400 ms) and short-term (3 s) windows.
//! Processing one block at a time: the ring buffer stores one f32 per callback,
//! not per sample, so the buffers stay tiny.

use std::f32::consts::PI;

struct Biquad {
    b: [f32; 3],
    a: [f32; 2],
    z: [[f32; 2]; 2], // z[ch][delay_line_stage]
}

impl Biquad {
    fn new(b: [f32; 3], a: [f32; 2]) -> Self {
        Self {
            b,
            a,
            z: [[0.0; 2]; 2],
        }
    }

    #[inline(always)]
    fn step(&mut self, x: f32, ch: usize) -> f32 {
        let y = self.b[0] * x + self.z[ch][0];
        self.z[ch][0] = self.b[1] * x - self.a[0] * y + self.z[ch][1];
        self.z[ch][1] = self.b[2] * x - self.a[1] * y;
        y
    }

    fn reset(&mut self) {
        self.z = [[0.0; 2]; 2];
    }
}

fn k_weighting_coeffs(sr: f32) -> ([f32; 3], [f32; 2], [f32; 3], [f32; 2]) {
    // Stage 1: high-shelf pre-filter (ITU-R BS.1770-4 §2.2)
    let f0 = 1681.974450955533_f32;
    let g = 3.999843853973347_f32;
    let q = 0.7071752369554196_f32;
    let k = (PI * f0 / sr).tan();
    let vh = 10.0_f32.powf(g / 20.0);
    let vb = vh.powf(0.4996667741545416_f32);
    let a0 = 1.0 + k / q + k * k;
    let pre_b = [
        (vh + vb * k / q + k * k) / a0,
        2.0 * (k * k - vh) / a0,
        (vh - vb * k / q + k * k) / a0,
    ];
    let pre_a = [2.0 * (k * k - 1.0) / a0, (1.0 - k / q + k * k) / a0];

    // Stage 2: high-pass RLB weighting
    let f1 = 38.13547087602444_f32;
    let q1 = 0.5003270373238773_f32;
    let k1 = (PI * f1 / sr).tan();
    let a0 = 1.0 + k1 / q1 + k1 * k1;
    let rlb_b = [1.0 / a0, -2.0 / a0, 1.0 / a0];
    let rlb_a = [2.0 * (k1 * k1 - 1.0) / a0, (1.0 - k1 / q1 + k1 * k1) / a0];

    (pre_b, pre_a, rlb_b, rlb_a)
}

struct BlockRingBuffer {
    buf: Vec<f32>,
    head: usize,
    sum: f64,
    fill: usize,
}

impl BlockRingBuffer {
    fn new(capacity: usize) -> Self {
        Self {
            buf: vec![0.0; capacity.max(1)],
            head: 0,
            sum: 0.0,
            fill: 0,
        }
    }

    fn push(&mut self, val: f32) {
        self.sum -= self.buf[self.head] as f64;
        self.buf[self.head] = val;
        self.sum += val as f64;
        self.head = (self.head + 1) % self.buf.len();
        if self.fill < self.buf.len() {
            self.fill += 1;
        }
    }

    fn mean(&self) -> f64 {
        if self.fill == 0 {
            0.0
        } else {
            self.sum / self.fill as f64
        }
    }

    fn reset(&mut self) {
        self.buf.fill(0.0);
        self.head = 0;
        self.sum = 0.0;
        self.fill = 0;
    }
}

pub struct KWeightedLUFS {
    pre: Biquad,
    rlb: Biquad,
    /// One block mean-square per ring-buffer slot
    momentary: BlockRingBuffer, // 400 ms ÷ block_dur blocks
    shortterm: BlockRingBuffer, // 3 s ÷ block_dur blocks
}

impl KWeightedLUFS {
    pub fn new(sr: u32, blocksize: usize) -> Self {
        let (pre_b, pre_a, rlb_b, rlb_a) = k_weighting_coeffs(sr as f32);
        let callbacks_per_s = sr as f64 / blocksize.max(1) as f64;
        let m_cap = ((0.4 * callbacks_per_s) as usize).max(1);
        let s_cap = ((3.0 * callbacks_per_s) as usize).max(1);
        Self {
            pre: Biquad::new(pre_b, pre_a),
            rlb: Biquad::new(rlb_b, rlb_a),
            momentary: BlockRingBuffer::new(m_cap),
            shortterm: BlockRingBuffer::new(s_cap),
        }
    }

    /// Process one interleaved stereo block; returns (momentary_LUFS, short_term_LUFS).
    pub fn process(&mut self, buf: &[f32]) -> (f32, f32) {
        let n = buf.len() / 2;
        let mut block_ms = 0.0_f32;

        for i in 0..n {
            let mut ms = 0.0_f32;
            for ch in 0..2 {
                let x = buf[i * 2 + ch];
                let y1 = self.pre.step(x, ch);
                let y2 = self.rlb.step(y1, ch);
                ms += y2 * y2;
            }
            block_ms += ms * 0.5;
        }
        block_ms /= n.max(1) as f32;

        self.momentary.push(block_ms);
        self.shortterm.push(block_ms);

        fn to_lufs(ms: f64) -> f32 {
            if ms < 1e-10 {
                f32::NEG_INFINITY
            } else {
                (-0.691 + 10.0 * ms.log10()) as f32
            }
        }

        (
            to_lufs(self.momentary.mean()),
            to_lufs(self.shortterm.mean()),
        )
    }

    pub fn reset(&mut self) {
        self.pre.reset();
        self.rlb.reset();
        self.momentary.reset();
        self.shortterm.reset();
    }
}
