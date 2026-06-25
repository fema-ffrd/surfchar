# surfchar
Surface characterization

## Developer install
Using conda or mamba:
```
$ conda env create -n surfchar
$ conda activate
(surfchar) $ conda env update -f environment.yml
```

Then, with the environment activated, install the surfchar package:
```
(surfchar) $ pip install -e .
```

## Running `surfchar`
After following the developer install instructions, simply run the `surfchar` command:
```
(surfchar) $ surfchar
```

View help and options:
```
(surfchar) $ surfchar --help
```

Run:
```
(surfchar) $ surfchar --hydro-enforced-dem ./dem.tif
```


