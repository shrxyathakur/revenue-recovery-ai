# Revenue Recovery AI
 
An autonomous payment degradation detection and recovery pipeline, built for Razorpay's /buildathon (AI Revenue Recovery track, Payment Degradation sub-problem).
 
Scope is intentionally bounded to three payment methods — **Card**, **UPI Intent**, and **E-mandate** — each chosen to exercise a distinct part of the architecture: Card for Hard Decline taxonomy, UPI for VPA handle join-key resolution, E-mandate for bank-code join with the Downtime API.
 
## How it works
 
Six stages, each reading the previous stage's output:
 
1. **Hard Decline pre-filter** — rule-based, exits permanently unrecoverable failures immediately
2. **Clustering + Downtime API correlation** — groups failure spikes by reason/step/timing, correlates against a live Downtime API for bank outage evidence
3. **Diagnosis** — two-path: deterministic table lookup for unambiguous reasons, LLM reasoning (with cluster context) reserved for genuinely uncertain ones
4. **Recovery action execution** — picks one bounded action per event based on its diagnosis
5. **Guardrails** — batch-level checks: uncertain-rate spikes, fraud compliance, per-entity retry circuit breaker
6. **Audit trail finalization** — turns the raw audit log into a readable summary report
Every stage writes to a shared, append-only audit log (`audit_log.jsonl`), so every decision — including every `uncertain` diagnosis and every guardrail override — is traceable after the fact.
 
## Why a two-path diagnosis, not an LLM classifying everything
 
Most reasons resolve to a bucket unambiguously just from Razorpay's own documentation text — no LLM needed, no cost, no hallucination risk. The LLM is reserved for the ~25% of cases that are genuinely ambiguous, where it reasons over real cluster corroboration (size, timing, Downtime API hit) instead of re-deriving something a lookup table already knows. `uncertain` is a first-class output, not a fallback — it always carries `resolution_pending_on`, stating what data would resolve it.
 
## Setup
 
1. Clone the repo and create a virtual environment
2. Install dependencies:
```
   pip install -r requirements.txt
```
3. (Optional, for live LLM diagnosis) create a `.env` or set an environment variable:
```
   GROQ_API_KEY=your_key
   STAGE2_LLM_MODE=live
```
   Without this, Stage 2 runs in `mock` mode by default — deterministic, no API calls, useful for development and demoing without burning tokens.
 
## Running the pipeline
 
Start the Downtime API mock server first, in its own terminal:
```
python .\mock-server\downtime_mock_server.py
```
 
Then, from the repo root, run all stages sequentially within a pipeline:
```
python run_pipeline.py
```
 
Synthetic test data is generated with:
```
python .\data\synthetic_data_generator.py
```
 
## Project structure
```
rev-rec-ai/
├── data/
│   ├── generated/              
│   └── synthetic_data_generator.py 
├── mock-server/
│   └── downtime_mock_server.py  
├── pipeline/
│   ├── stage0_hard_decline.py    
│   ├── stage1_detection.py      
│   ├── stage2_diagnosis.py        
│   ├── stage3_recovery.py        
│   ├── stage4_guardrails.py       
│   └── stage5_audit_finalization.py 
├── .env                          
├── .gitignore                    
├── audit_log.jsonl               
├── audit_trail.py                 
├── README.md                      
├── requirements.txt              
└── run_pipeline.py               
 ```
## Known limitations
 
- UPI Category B VPA handles (e.g. `@paytm`, `@ybl`) resolve only to a PSP, not the underlying bank — these events cluster on reason/timing alone, at coarser resolution than Card/E-mandate.
- The Downtime API mock server's field names are a reasonable approximation of Razorpay's real API, not verified 1:1 against the live schema (sandbox access requires KYC we don't have).
- The Stage 4 entity retry circuit breaker caps retries per entity across a processed batch, not a true sliding time window — a real limitation for streaming/live use.
